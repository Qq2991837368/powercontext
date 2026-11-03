import { env, pipeline } from '@huggingface/transformers';

const scope: any = self as any;

const DEFAULT_MODEL = 'Xenova/bge-small-zh-v1.5';
const REMOTE_HOSTS = ['https://hf-mirror.com', 'https://huggingface.co'];

type Dtype = 'q8' | 'fp32';
type DeviceKind = 'webgpu' | 'wasm';

type InitMessage = {
  type: 'init';
  model?: string;
  dtype: Dtype;
  device: 'webgpu' | 'wasm';
  hosts?: string[];
};

type EmbedMessage = {
  type: 'embed';
  requestId: number;
  texts: string[];
  batchSize?: number;
};

type WorkerMessage = InitMessage | EmbedMessage;

let extractor: any = null;
let ready = false;

const post = (message: Record<string, unknown>) => scope.postMessage(message);

function isModelFile(name: string): boolean {
  const isModelPath = name.includes('/resolve/') || name.includes('/models/');
  const isFile = /\.onnx(?:$|\?)|tokenizer\.json|tokenizer_config\.json|config\.json|special_tokens_map\.json/.test(
    name
  );
  return isModelPath && isFile;
}

function snapshotResourceNames(): Set<string> {
  try {
    return new Set(performance.getEntriesByType('resource').map((e) => e.name));
  } catch {
    return new Set();
  }
}

function collectModelFiles(before: Set<string>) {
  try {
    const entries = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const files = entries
      .filter((e) => !before.has(e.name) && isModelFile(e.name))
      .map((e) => ({
        name: decodeURIComponent(e.name.split('/').slice(-2).join('/')),
        bytes: e.decodedBodySize || e.transferSize || 0,
      }))
      .filter((f) => f.bytes > 0);
    const modelBytes = files.reduce((sum, f) => sum + f.bytes, 0);
    return { files, modelBytes, fromResourceTiming: modelBytes > 0 };
  } catch {
    return { files: [], modelBytes: 0, fromResourceTiming: false };
  }
}

async function probeHost(host: string, model: string): Promise<boolean> {
  try {
    const url = `${host}/${model}/resolve/main/config.json`;
    const res = await fetch(url, { cache: 'force-cache' });
    return res.ok;
  } catch {
    return false;
  }
}

async function hasLocalModel(model: string): Promise<boolean> {
  try {
    const res = await fetch(`/models/${model}/config.json`, { cache: 'no-store' });
    return res.ok;
  } catch {
    return false;
  }
}

async function chooseSource(model: string, hosts: string[]) {
  if (await hasLocalModel(model)) {
    env.allowLocalModels = true;
    env.allowRemoteModels = false;
    env.localModelPath = '/models/';
    return { host: 'local:/models/', localModel: true };
  }
  env.allowLocalModels = false;
  env.allowRemoteModels = true;
  for (const host of hosts) {
    if (await probeHost(host, model)) {
      env.remoteHost = host;
      return { host, localModel: false };
    }
  }
  // Let transformers.js try the default host so the error is meaningful.
  env.remoteHost = hosts[0];
  return { host: hosts[0], localModel: false };
}

async function hasWebGPU(): Promise<{ ok: boolean; reason?: string }> {
  const nav = navigator as Navigator & { gpu?: { requestAdapter(): Promise<unknown> } };
  if (!nav.gpu) return { ok: false, reason: '当前浏览器未暴露 navigator.gpu' };
  try {
    const adapter = await nav.gpu.requestAdapter();
    if (!adapter) return { ok: false, reason: '无法获取 WebGPU 适配器' };
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: `WebGPU 适配器初始化失败：${String(error)}` };
  }
}

async function loadPipeline(
  model: string,
  dtype: Dtype,
  device: DeviceKind
): Promise<{ loadMs: number; warmupMs: number }> {
  const t0 = performance.now();
  extractor = await pipeline('feature-extraction', model, {
    device,
    dtype,
    progress_callback: (progress: any) => {
      if (progress?.status === 'progress' && typeof progress.progress === 'number') {
        post({
          type: 'progress',
          file: progress.file,
          progress: progress.progress,
          loaded: progress.loaded,
          total: progress.total,
        });
      }
    },
  });
  const loadMs = performance.now() - t0;
  const t1 = performance.now();
  const warmup = await extractor(['测试文本'], { pooling: 'cls', normalize: true });
  warmup.tolist();
  const warmupMs = performance.now() - t1;
  return { loadMs, warmupMs };
}

async function handleInit(msg: InitMessage) {
  const model = msg.model || DEFAULT_MODEL;
  const before = snapshotResourceNames();
  extractor = null;
  ready = false;
  await configureOrt().catch(() => undefined);

  const source = await chooseSource(model, msg.hosts?.length ? msg.hosts : REMOTE_HOSTS);

  let device: DeviceKind = 'wasm';
  let fallbackReason: string | null = null;

  if (msg.device === 'webgpu') {
    const gpu = await hasWebGPU();
    if (!gpu.ok) {
      fallbackReason = gpu.reason || 'WebGPU 不可用';
    } else if (msg.dtype === 'q8') {
      // Measured on this stack: q8 on ORT WebGPU drops pure-vector Recall@5 from
      // 80.1% to 21.8% and is 5x slower than FP32 on WebGPU (int8 kernels fall
      // back inside the WebGPU EP). Keep WebGPU for FP32, run INT8 on WASM.
      fallbackReason =
        'INT8（q8）量化算子在 ONNX Runtime Web 的 WebGPU EP 上数值不可靠（实测纯向量 Recall@5 21.8% vs WASM 80.1%），已自动改用 WASM 后端以保证检索质量。';
    } else {
      try {
        const timing = await loadPipeline(model, msg.dtype, 'webgpu');
        device = 'webgpu';
        ready = true;
        const sizes = collectModelFiles(before);
        post({
          type: 'ready',
          dtype: msg.dtype,
          device,
          requestedDevice: msg.device,
          actualDevice: device,
          fallbackReason: null,
          host: source.host,
          localModel: source.localModel,
          ...timing,
          ...sizes,
        });
        return;
      } catch (error) {
        fallbackReason = `WebGPU 推理失败，已自动降级：${String(error).slice(0, 220)}`;
        extractor = null;
      }
    }
  }

  const timing = await loadPipeline(model, msg.dtype, 'wasm');
  ready = true;
  const sizes = collectModelFiles(before);
  post({
    type: 'ready',
    dtype: msg.dtype,
    device,
    requestedDevice: msg.device,
    actualDevice: device,
    fallbackReason,
    host: source.host,
    localModel: source.localModel,
    ...timing,
    ...sizes,
  });
}

async function handleEmbed(msg: EmbedMessage) {
  if (!extractor || !ready) throw new Error('模型尚未加载完成');
  const batchSize = Math.max(1, Math.min(32, msg.batchSize || 8));
  const t0 = performance.now();
  const vectors: number[][] = [];
  for (let i = 0; i < msg.texts.length; i += batchSize) {
    const batch = msg.texts.slice(i, i + batchSize);
    const output = await extractor(batch, { pooling: 'cls', normalize: true });
    vectors.push(...output.tolist());
    post({
      type: 'embed-progress',
      requestId: msg.requestId,
      done: Math.min(i + batch.length, msg.texts.length),
      total: msg.texts.length,
    });
  }
  const ms = performance.now() - t0;
  post({
    type: 'embedded',
    requestId: msg.requestId,
    vectors,
    ms,
    docsPerSecond: msg.texts.length / (ms / 1000),
  });
}

scope.onmessage = async (event: MessageEvent<WorkerMessage>) => {
  const msg = event.data;
  try {
    if (msg.type === 'init') {
      await handleInit(msg);
    } else if (msg.type === 'embed') {
      await handleEmbed(msg);
    }
  } catch (error) {
    post({
      type: 'error',
      scope: msg.type,
      requestId: 'requestId' in msg ? msg.requestId : undefined,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};

// Point ONNX Runtime Web to locally-served wasm assets (copied by the Vite plugin).
// ORT dynamically imports its `.mjs` loader; Vite refuses to import a file from
// /public (it throws "should not be imported from source code"), so we fetch the
// loader as text and hand ORT a Blob URL instead. The .wasm binary uses an
// absolute same-origin URL, which also works for the production build.
async function configureOrt(): Promise<void> {
  const backend = (env.backends as any)?.onnx;
  if (!backend?.wasm) return;
  backend.wasm.numThreads = (scope as any).crossOriginIsolated
    ? Math.min(4, navigator.hardwareConcurrency || 1)
    : 1;

  const base = new URL('/ort/', scope.location.origin);
  const mjsName = 'ort-wasm-simd-threaded.jsep.mjs';
  const wasmName = 'ort-wasm-simd-threaded.jsep.wasm';
  const wasmUrl = new URL(wasmName, base).href;

  try {
    const res = await fetch(new URL(mjsName, base), { cache: 'force-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    const blobUrl = URL.createObjectURL(new Blob([text], { type: 'text/javascript' }));
    backend.wasm.wasmPaths = { mjs: blobUrl, wasm: wasmUrl };
    return;
  } catch {
    // Fall back to plain URLs; ORT will fetch them directly.
    backend.wasm.wasmPaths = { mjs: new URL(mjsName, base).href, wasm: wasmUrl };
  }
}
