import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { runBenchmark, type BenchProgress } from './bench/runBenchmark';
import { CORPUS, MODEL_MANIFEST } from './data/corpus';
import { EmbeddingClient } from './embedding/client';
import { charCount, chunkText, type Chunk } from './lib/chunk';
import { formatBytes, formatMs, median } from './lib/metrics';
import type {
  BenchmarkReport,
  DeviceKind,
  Dtype,
  HealthInfo,
  KbInfo,
  ModelReadyInfo,
  QueryTiming,
  SearchMode,
  SearchResponse,
  SessionImportMetrics,
} from './types';

const MODEL = MODEL_MANIFEST.model;
const ACTIVE_KB_KEY = 'stepfun:activeKb';
const BENCH_KEY = 'stepfun:benchmark';

type Phase = 'idle' | 'embedding' | 'writing' | 'refreshing' | 'done' | 'error';

interface ImportProgress {
  done: number;
  total: number;
  message: string;
}

function StatusPill(props: { tone: 'ok' | 'warn' | 'info' | 'muted'; label: string; value: string }) {
  return (
    <div className={`pill pill-${props.tone}`}>
      <span className="pill-label">{props.label}</span>
      <span className="pill-value">{props.value}</span>
    </div>
  );
}

function Section(props: { title: string; subtitle?: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>{props.title}</h2>
          {props.subtitle ? <p className="panel-subtitle">{props.subtitle}</p> : null}
        </div>
        {props.actions ? <div className="panel-actions">{props.actions}</div> : null}
      </header>
      <div className="panel-body">{props.children}</div>
    </section>
  );
}

function Highlight(props: { text: string; terms: string[] }) {
  const terms = props.terms.filter((t) => t.length >= 2).map((t) => t.toLowerCase());
  if (!terms.length) return <>{props.text}</>;
  const parts = props.text.split(new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi'));
  return (
    <>
      {parts.map((part, index) =>
        terms.includes(part.toLowerCase()) ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>
      )}
    </>
  );
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function App() {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [kbs, setKbs] = useState<KbInfo[]>([]);
  const [activeKbId, setActiveKbId] = useState<string | null>(() => localStorage.getItem(ACTIVE_KB_KEY));
  const [dtype, setDtype] = useState<Dtype>('q8');
  const [devicePref, setDevicePref] = useState<'auto' | 'wasm'>('auto');
  const [modelStatus, setModelStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [modelInfo, setModelInfo] = useState<ModelReadyInfo | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  const [loadProgress, setLoadProgress] = useState<{ file?: string; progress: number } | null>(null);

  const modelsRef = useRef(new Map<Dtype, { client: EmbeddingClient; info: ModelReadyInfo }>());
  const inflightRef = useRef(new Map<Dtype, Promise<{ client: EmbeddingClient; info: ModelReadyInfo }>>());
  const firstDeviceEffect = useRef(true);

  const [importText, setImportText] = useState('');
  const [files, setFiles] = useState<{ name: string; text: string }[]>([]);
  const [chunkSize, setChunkSize] = useState(320);
  const [overlap, setOverlap] = useState(64);
  const [phase, setPhase] = useState<Phase>('idle');
  const [progress, setProgress] = useState<ImportProgress>({ done: 0, total: 0, message: '' });
  const [importError, setImportError] = useState<string | null>(null);
  const [lastImport, setLastImport] = useState<SessionImportMetrics | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [query, setQuery] = useState('端侧量化后怎么保证检索质量？');
  const [mode, setMode] = useState<SearchMode>('hybrid');
  const [nResults, setNResults] = useState(8);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [lastSearch, setLastSearch] = useState<(SearchResponse & { e2eMs: number }) | null>(null);
  const [timings, setTimings] = useState<QueryTiming[]>([]);

  const [benchStatus, setBenchStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [benchProgress, setBenchProgress] = useState<BenchProgress | null>(null);
  const [benchError, setBenchError] = useState<string | null>(null);
  const [benchReport, setBenchReport] = useState<BenchmarkReport | null>(() => {
    const stored = localStorage.getItem(BENCH_KEY);
    if (!stored) return null;
    try {
      return JSON.parse(stored) as BenchmarkReport;
    } catch {
      return null;
    }
  });
  const [toast, setToast] = useState<string | null>(null);

  const activeKb = useMemo(() => kbs.find((kb) => kb.id === activeKbId) || null, [kbs, activeKbId]);

  const ensureModel = useCallback(
    async (targetDtype: Dtype): Promise<{ client: EmbeddingClient; info: ModelReadyInfo }> => {
      const cached = modelsRef.current.get(targetDtype);
      if (cached) return cached;
      const inflight = inflightRef.current.get(targetDtype);
      if (inflight) return inflight;

      const task = (async () => {
        setModelError(null);
        setModelStatus('loading');
        setLoadProgress({ progress: 0 });
        const client = new EmbeddingClient();
        try {
          const info = await client.init({
            dtype: targetDtype,
            device: devicePref === 'wasm' ? 'wasm' : 'webgpu',
            model: MODEL,
            onProgress: (p) => setLoadProgress(p),
          });
          const handle = { client, info };
          modelsRef.current.set(targetDtype, handle);
          setModelInfo(info);
          setModelStatus('ready');
          return handle;
        } catch (error) {
          client.terminate();
          setModelStatus('error');
          setModelError(error instanceof Error ? error.message : String(error));
          throw error;
        } finally {
          setLoadProgress(null);
          inflightRef.current.delete(targetDtype);
        }
      })();
      inflightRef.current.set(targetDtype, task);
      return task;
    },
    [devicePref]
  );

  const refreshKbs = useCallback(async (preferredId?: string) => {
    const list = await api.listKbs();
    setKbs(list);
    if (preferredId) {
      setActiveKbId(preferredId);
      localStorage.setItem(ACTIVE_KB_KEY, preferredId);
    }
    return list;
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [healthInfo, list] = await Promise.all([
        api.health().catch(() => null),
        api.listKbs().catch(() => [] as KbInfo[]),
      ]);
      if (cancelled) return;
      setHealth(healthInfo);
      setKbs(list);
      const stored = localStorage.getItem(ACTIVE_KB_KEY);
      const restored = list.find((kb) => kb.id === stored);
      const initialDtype = (restored?.dtype as Dtype) || 'q8';
      if (restored) setActiveKbId(restored.id);
      setDtype(initialDtype);
      ensureModel(initialDtype).catch(() => undefined);
    })();
    return () => {
      cancelled = true;
    };
  }, [ensureModel]);

  useEffect(() => {
    if (firstDeviceEffect.current) {
      firstDeviceEffect.current = false;
      return;
    }
    for (const handle of modelsRef.current.values()) handle.client.terminate();
    modelsRef.current.clear();
    inflightRef.current.clear();
    setModelInfo(null);
    ensureModel(dtype).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [devicePref]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  const sourceItems = useMemo(() => {
    const items: { text: string; source: string }[] = [];
    for (const file of files) items.push({ text: file.text, source: file.name });
    if (importText.trim()) items.push({ text: importText, source: '粘贴文本' });
    return items;
  }, [files, importText]);

  const previewChunks = useMemo(
    () =>
      sourceItems.flatMap((item) =>
        chunkText(item.text, { size: chunkSize, overlap, source: item.source })
      ),
    [sourceItems, chunkSize, overlap]
  );

  const canImport =
    previewChunks.length > 0 &&
    phase !== 'embedding' &&
    phase !== 'writing' &&
    phase !== 'refreshing' &&
    modelStatus !== 'loading';

  async function handleSelectKb(id: string) {
    setActiveKbId(id);
    localStorage.setItem(ACTIVE_KB_KEY, id);
    setLastSearch(null);
    const kb = kbs.find((item) => item.id === id);
    if (kb && (kb.dtype === 'q8' || kb.dtype === 'fp32')) {
      setDtype(kb.dtype);
      await ensureModel(kb.dtype).catch(() => undefined);
    }
  }

  async function handleCreateKb() {
    try {
      const kb = await api.createKb(
        `${dtype === 'q8' ? 'INT8' : 'FP32'} 知识库 ${new Date().toLocaleTimeString()}`,
        dtype,
        MODEL
      );
      await refreshKbs(kb.id);
      setToast(`已新建知识库 ${kb.id}`);
    } catch (error) {
      setToast(`新建失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function handleDeleteKb() {
    if (!activeKb) return;
    if (!window.confirm(`确认删除知识库「${activeKb.name}」及其全部向量？`)) return;
    await api.deleteKb(activeKb.id);
    localStorage.removeItem(ACTIVE_KB_KEY);
    setActiveKbId(null);
    setLastSearch(null);
    await refreshKbs();
    setToast('知识库已删除');
  }

  async function handleImport() {
    if (!previewChunks.length) return;
    setImportError(null);
    setPhase('embedding');
    setProgress({ done: 0, total: previewChunks.length, message: '加载模型并准备向量化' });
    const started = performance.now();
    try {
      const handle = await ensureModel(dtype);
      let kb = activeKb && activeKb.dtype === dtype ? activeKb : null;
      if (!kb) {
        kb = await api.createKb(
          `${dtype === 'q8' ? 'INT8' : 'FP32'} 知识库 ${new Date().toLocaleTimeString()}`,
          dtype,
          MODEL
        );
        setActiveKbId(kb.id);
        localStorage.setItem(ACTIVE_KB_KEY, kb.id);
      }

      const embedded = await handle.client.embed(
        previewChunks.map((chunk) => chunk.text),
        {
          batchSize: 8,
          onProgress: (done, total) =>
            setProgress({ done, total, message: '浏览器本地向量化（Web Worker）' }),
        }
      );

      setPhase('writing');
      const runId = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      const documents = previewChunks.map((chunk, index) => ({
        id: `${runId}_${String(index).padStart(3, '0')}`,
        text: chunk.text,
        embedding: embedded.vectors[index],
        metadata: {
          source: chunk.source,
          chunkIndex: chunk.chunkIndex,
          chars: chunk.text.length,
          importedAt: new Date().toISOString(),
        },
      }));

      let writeMs = 0;
      const WRITE_BATCH = 16;
      for (let index = 0; index < documents.length; index += WRITE_BATCH) {
        const batch = documents.slice(index, index + WRITE_BATCH);
        const result = await api.addDocuments(kb.id, batch, false);
        writeMs += result.writeMs;
        setProgress({
          done: Math.min(index + batch.length, documents.length),
          total: documents.length,
          message: `写入 seekdb（${Math.min(index + batch.length, documents.length)}/${documents.length}）`,
        });
      }

      setPhase('refreshing');
      setProgress({ done: documents.length, total: documents.length, message: '刷新向量索引' });
      const refreshResult = await api.refresh(kb.id);
      const totalMs = performance.now() - started;

      setLastImport({
        at: new Date().toISOString(),
        dtype,
        device: handle.info.device,
        docs: documents.length,
        chunks: previewChunks.length,
        chars: charCount(previewChunks),
        embedMs: embedded.ms,
        writeMs,
        refreshMs: refreshResult.refreshMs,
        totalMs,
        docsPerSecond: embedded.docsPerSecond,
      });
      setPhase('done');
      setToast(`已写入 ${documents.length} 个片段，索引刷新完成，新内容可检索`);
      await refreshKbs(kb.id);
    } catch (error) {
      setPhase('error');
      setImportError(error instanceof Error ? error.message : String(error));
    }
  }

  async function handleRefreshIndex() {
    if (!activeKb) return;
    setRefreshing(true);
    try {
      const result = await api.refresh(activeKb.id);
      await refreshKbs();
      setToast(`索引刷新完成：${formatMs(result.refreshMs)}，共 ${result.count} 条`);
    } catch (error) {
      setToast(`刷新失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRefreshing(false);
    }
  }

  async function handleSearch() {
    if (!query.trim()) return;
    if (!activeKb) {
      setSearchError('请先创建或选择知识库');
      return;
    }
    setSearching(true);
    setSearchError(null);
    const started = performance.now();
    try {
      const kbDtype = (activeKb.dtype === 'fp32' ? 'fp32' : 'q8') as Dtype;
      const handle = await ensureModel(kbDtype);
      const embedded = await handle.client.embed([query], { batchSize: 1 });
      const response = await api.search(activeKb.id, {
        query,
        embedding: embedded.vectors[0],
        mode,
        nResults,
      });
      const e2eMs = performance.now() - started;
      setLastSearch({ ...response, e2eMs });
      const timing: QueryTiming = {
        at: new Date().toISOString(),
        embedMs: embedded.ms,
        searchMs: response.searchMs,
        e2eMs,
      };
      setTimings((prev) => [...prev.slice(-19), timing]);
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  }

  async function handleBenchmark() {
    setBenchStatus('running');
    setBenchError(null);
    setBenchProgress({ step: '准备评测', detail: '固定语料与查询集，逐项改变精度和策略', percent: 1 });
    try {
      const report = await runBenchmark({
        ensureModel,
        onProgress: (value) => setBenchProgress(value),
      });
      setBenchReport(report);
      localStorage.setItem(BENCH_KEY, JSON.stringify(report));
      setBenchStatus('done');
      await refreshKbs();
    } catch (error) {
      setBenchStatus('error');
      setBenchError(error instanceof Error ? error.message : String(error));
    }
  }

  function handleLoadSample() {
    setFiles([]);
    setImportText(
      CORPUS.map((doc) => `${doc.title}\n${doc.text}`).join('\n\n')
    );
    setToast(`已载入内置示例语料（${CORPUS.length} 篇）`);
  }

  const importPercent =
    progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
  const queryMedians = {
    embed: median(timings.map((item) => item.embedMs)),
    search: median(timings.map((item) => item.searchMs)),
    e2e: median(timings.map((item) => item.e2eMs)),
  };
  const modelBytes = modelInfo ? MODEL_MANIFEST.dtype[modelInfo.dtype].total : 0;
  const wasmNotice =
    modelInfo?.device === 'wasm'
      ? modelInfo.fallbackReason ||
        (devicePref === 'wasm' ? '已按设置强制使用 WASM，用于验证降级路径。' : '当前环境使用 WASM 后端。')
      : null;

  return (
    <div className="app">
      <header className="hero">
        <div>
          <div className="eyebrow">seekdb × StepFun · 端侧 AI 推理与混合检索</div>
          <h1>浏览器本地向量化的迷你知识库</h1>
          <p className="hero-sub">
            文本在浏览器内由 bge-small-zh-v1.5 编码，向量与原文写入嵌入式 seekdb，向量 + 全文混合检索由
            DBMS_HYBRID_SEARCH（RRF）在数据库内完成。
          </p>
        </div>
        <div className="pill-grid">
          <StatusPill
            tone={health?.ok ? 'ok' : 'warn'}
            label="seekdb"
            value={health ? `嵌入式 · ${health.database}` : '连接中'}
          />
          <StatusPill
            tone={modelInfo?.device === 'webgpu' ? 'ok' : modelInfo?.device === 'wasm' ? 'warn' : 'muted'}
            label="推理后端"
            value={
              modelStatus === 'loading'
                ? '加载中'
                : modelInfo
                  ? modelInfo.device === 'webgpu'
                    ? 'WebGPU'
                    : 'WASM（自动降级）'
                  : '待加载'
            }
          />
          <StatusPill
            tone="info"
            label="模型精度"
            value={modelInfo ? `${modelInfo.dtype === 'q8' ? 'INT8' : 'FP32'} · ${formatBytes(modelBytes)}` : '—'}
          />
          <StatusPill
            tone="muted"
            label="模型加载"
            value={modelInfo ? formatMs(modelInfo.loadMs) : modelStatus === 'loading' ? '下载 / 初始化中' : '—'}
          />
        </div>
      </header>

      {wasmNotice ? (
        <div className="banner banner-warn">
          <strong>已降级到 WASM</strong>
          <span>{wasmNotice}</span>
          <span className="banner-note">
            {modelInfo?.dtype === 'q8'
              ? 'FP32 模式会优先使用 WebGPU；INT8 固定使用 WASM 以保证量化推理的数值正确性。'
              : '核心流程仍可运行；WebGPU 可用时会自动优先使用 GPU。'}
          </span>
        </div>
      ) : null}
      {modelStatus === 'loading' ? (
        <div className="banner banner-info">
          <strong>正在加载模型</strong>
          <span>
            {loadProgress?.file ? `${loadProgress.file} · ` : ''}
            {Math.round((loadProgress?.progress || 0) * 100)}%
          </span>
          <span className="banner-note">模型文件经 hf-mirror / Hugging Face 下载，推理全部在浏览器本地执行。</span>
        </div>
      ) : null}
      {modelError ? (
        <div className="banner banner-error">
          <strong>模型加载失败</strong>
          <span>{modelError}</span>
        </div>
      ) : null}

      <main className="layout">
        <div className="column">
          <Section
            title="知识库与会话"
            subtitle="集合状态、精度与索引控制"
            actions={
              <>
                <button className="btn ghost" onClick={handleCreateKb}>
                  新建知识库
                </button>
                <button className="btn ghost danger" onClick={handleDeleteKb} disabled={!activeKb}>
                  删除
                </button>
              </>
            }
          >
            <div className="field">
              <label>当前知识库</label>
              <select
                value={activeKbId || ''}
                onChange={(event) => handleSelectKb(event.target.value)}
              >
                <option value="">未选择</option>
                {kbs.map((kb) => (
                  <option key={kb.id} value={kb.id}>
                    {kb.name} · {kb.count} 条 · {kb.dtype.toUpperCase()}
                  </option>
                ))}
              </select>
              <p className="hint">
                {activeKb
                  ? `集合 ${activeKb.id} · 维度 ${activeKb.dimension} · 距离 ${activeKb.distance} · 文档 ${activeKb.count}`
                  : '首次导入时会自动创建集合。'}
              </p>
            </div>

            <div className="row">
              <div className="field">
                <label>新导入精度</label>
                <select value={dtype} onChange={(event) => setDtype(event.target.value as Dtype)}>
                  <option value="q8">INT8（q8，默认）</option>
                  <option value="fp32">FP32（质量基线）</option>
                </select>
              </div>
              <div className="field">
                <label>推理后端策略</label>
                <select
                  value={devicePref}
                  onChange={(event) => setDevicePref(event.target.value as 'auto' | 'wasm')}
                >
                  <option value="auto">优先 WebGPU（INT8 自动使用 WASM）</option>
                  <option value="wasm">强制 WASM（验证降级路径）</option>
                </select>
              </div>
            </div>
            {activeKb && activeKb.dtype !== dtype ? (
              <p className="hint warn-text">
                当前知识库为 {activeKb.dtype.toUpperCase()}，导入将新建一个 {dtype.toUpperCase()} 知识库，避免不同精度的向量混用。
              </p>
            ) : null}

            <div className="row">
              <button className="btn" onClick={handleRefreshIndex} disabled={!activeKb || refreshing}>
                {refreshing ? '刷新中…' : '刷新向量索引'}
              </button>
              <button
                className="btn ghost"
                onClick={() => {
                  setImportText('');
                  setFiles([]);
                  setPhase('idle');
                  setProgress({ done: 0, total: 0, message: '' });
                }}
              >
                清空导入区
              </button>
            </div>
          </Section>

          <Section
            title="导入语料"
            subtitle={`${previewChunks.length} 个片段 · ${charCount(previewChunks)} 字`}
            actions={
              <button className="btn ghost" onClick={handleLoadSample}>
                载入示例语料
              </button>
            }
          >
            <div className="field">
              <label>粘贴文本</label>
              <textarea
                rows={6}
                value={importText}
                onChange={(event) => setImportText(event.target.value)}
                placeholder="粘贴文章、笔记或任意中文文本，导入时会自动分块并在浏览器本地向量化。"
              />
            </div>
            <div className="field">
              <label>上传文本文件（.txt / .md）</label>
              <input
                type="file"
                multiple
                accept=".txt,.md,.markdown,text/plain,text/markdown"
                onChange={async (event) => {
                  const selected = Array.from(event.target.files || []);
                  const loaded = await Promise.all(
                    selected.map(async (file) => ({ name: file.name, text: await file.text() }))
                  );
                  setFiles((prev) => [...prev, ...loaded]);
                  event.target.value = '';
                }}
              />
              {files.length ? (
                <ul className="file-list">
                  {files.map((file) => (
                    <li key={file.name}>
                      <span>{file.name}</span>
                      <span>{file.text.length} 字</span>
                      <button
                        className="link-btn"
                        onClick={() => setFiles((prev) => prev.filter((item) => item.name !== file.name))}
                      >
                        移除
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="row">
              <div className="field">
                <label>分块大小（字符）</label>
                <input
                  type="number"
                  min={80}
                  max={1000}
                  value={chunkSize}
                  onChange={(event) => setChunkSize(Number(event.target.value) || 320)}
                />
              </div>
              <div className="field">
                <label>重叠字符</label>
                <input
                  type="number"
                  min={0}
                  max={200}
                  value={overlap}
                  onChange={(event) => setOverlap(Number(event.target.value) || 0)}
                />
              </div>
            </div>

            <div className="progress-box">
              <div className="progress-track">
                <div
                  className={`progress-fill phase-${phase}`}
                  style={{ width: `${phase === 'done' ? 100 : importPercent}%` }}
                />
              </div>
              <div className="progress-meta">
                <span>
                  {phase === 'idle' && '等待导入'}
                  {phase === 'embedding' && `本地向量化 ${progress.done}/${progress.total}`}
                  {phase === 'writing' && `写入 seekdb ${progress.done}/${progress.total}`}
                  {phase === 'refreshing' && '刷新向量索引'}
                  {phase === 'done' && '导入完成，新内容已可检索'}
                  {phase === 'error' && '导入失败'}
                </span>
                <span>{progress.message}</span>
              </div>
            </div>
            {importError ? <div className="inline-error">{importError}</div> : null}

            <div className="row">
              <button className="btn primary" onClick={handleImport} disabled={!canImport}>
                {phase === 'embedding' || phase === 'writing' || phase === 'refreshing'
                  ? '导入中…'
                  : `向量化并写入 ${previewChunks.length} 个片段`}
              </button>
            </div>
            <p className="hint">
              写入策略：16 条/批增量 upsert，全部写入完成后调用 <code>refresh_index()</code>，确保新增片段立即可检索。
            </p>
          </Section>
        </div>

        <div className="column">
          <Section
            title="混合检索"
            subtitle="查询向量在浏览器生成，融合排序在 seekdb 内完成"
            actions={
              <div className="segmented">
                {(['hybrid', 'vector', 'fulltext'] as SearchMode[]).map((value) => (
                  <button
                    key={value}
                    className={mode === value ? 'active' : ''}
                    onClick={() => setMode(value)}
                  >
                    {value === 'hybrid' ? '混合' : value === 'vector' ? '仅向量' : '仅全文'}
                  </button>
                ))}
              </div>
            }
          >
            <div className="search-bar">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') handleSearch();
                }}
                placeholder="输入自然语言问题，例如：量化后检索质量怎么评估？"
              />
              <select value={nResults} onChange={(event) => setNResults(Number(event.target.value))}>
                {[5, 8, 10, 20].map((value) => (
                  <option key={value} value={value}>
                    Top {value}
                  </option>
                ))}
              </select>
              <button className="btn primary" onClick={handleSearch} disabled={searching || !activeKb}>
                {searching ? '检索中…' : '检索'}
              </button>
            </div>
            {searchError ? <div className="inline-error">{searchError}</div> : null}
            {lastSearch ? (
              <div className="search-meta">
                <span>查询向量化 {formatMs(lastSearch.e2eMs - lastSearch.searchMs)}</span>
                <span>seekdb 检索 {formatMs(lastSearch.searchMs)}</span>
                <span>端到端 {formatMs(lastSearch.e2eMs)}</span>
                <span>命中 {lastSearch.results.length} 条</span>
                {lastSearch.terms.length ? <span>全文词项 {lastSearch.terms.join(' / ')}</span> : null}
              </div>
            ) : null}

            <div className="results">
              {lastSearch?.results.map((hit, index) => (
                <article className="result" key={hit.id}>
                  <div className="result-head">
                    <span className="rank">{index + 1}</span>
                    <span className="badge">{String(hit.metadata?.source || '文档')}</span>
                    <span className="mono">{hit.id}</span>
                    <span className="distance">
                      {hit.distance !== null ? `score ${hit.distance.toFixed(4)}` : '—'}
                    </span>
                  </div>
                  <p className="result-text">
                    <Highlight text={hit.text} terms={lastSearch.terms} />
                  </p>
                </article>
              ))}
              {lastSearch && !lastSearch.results.length ? (
                <div className="empty">没有命中结果，尝试更换关键词或先导入语料。</div>
              ) : null}
              {!lastSearch ? (
                <div className="empty">
                  导入语料后输入问题。混合检索会同时走向量索引与全文索引，并由 seekdb 的 RRF 融合返回排序。
                </div>
              ) : null}
            </div>
          </Section>

          <Section title="本次会话指标" subtitle="首次加载与预热后的请求分开记录">
            <div className="metric-grid">
              <div className="metric">
                <span>模型 / 后端</span>
                <strong>
                  {modelInfo
                    ? `${modelInfo.dtype === 'q8' ? 'INT8' : 'FP32'} · ${modelInfo.device.toUpperCase()}`
                    : '—'}
                </strong>
                <em>{modelInfo ? `加载 ${formatMs(modelInfo.loadMs)} · 预热 ${formatMs(modelInfo.warmupMs)}` : '—'}</em>
              </div>
              <div className="metric">
                <span>模型体积</span>
                <strong>{modelBytes ? formatBytes(modelBytes) : '—'}</strong>
                <em>{modelInfo?.fromResourceTiming ? '浏览器资源计时实测' : '内置清单口径'}</em>
              </div>
              <div className="metric">
                <span>最近导入</span>
                <strong>{lastImport ? `${lastImport.docs} 片段` : '—'}</strong>
                <em>
                  {lastImport
                    ? `向量化 ${formatMs(lastImport.embedMs)} · 写入 ${formatMs(lastImport.writeMs)} · 刷新 ${formatMs(lastImport.refreshMs)}`
                    : '尚无导入'}
                </em>
              </div>
              <div className="metric">
                <span>向量化吞吐</span>
                <strong>{lastImport ? `${lastImport.docsPerSecond.toFixed(2)} 片段/秒` : '—'}</strong>
                <em>{lastImport ? `${lastImport.chars} 字 · 端到端 ${formatMs(lastImport.totalMs)}` : '—'}</em>
              </div>
              <div className="metric">
                <span>查询延迟（中位数）</span>
                <strong>{timings.length ? formatMs(queryMedians.e2e) : '—'}</strong>
                <em>
                  {timings.length
                    ? `向量化 ${formatMs(queryMedians.embed)} · 检索 ${formatMs(queryMedians.search)} · ${timings.length} 次`
                    : '检索一次后记录'}
                </em>
              </div>
              <div className="metric">
                <span>seekdb 集合</span>
                <strong>{health ? `${health.collectionCount} 个` : '—'}</strong>
                <em>{health ? `${health.path}` : '—'}</em>
              </div>
            </div>
            <div className="row">
              <button
                className="btn ghost"
                onClick={() => downloadJson('stepfun-session-metrics.json', { health, modelInfo, lastImport, timings })}
              >
                导出会话指标 JSON
              </button>
            </div>
          </Section>
        </div>
      </main>

      <Section
        title="量化与性能对比评测"
        subtitle="固定语料、固定查询集、同一设备；INT8 与 FP32 分别建库，报告 Recall@5 / nDCG@5 与分阶段耗时"
        actions={
          <div className="row tight">
            <button className="btn primary" onClick={handleBenchmark} disabled={benchStatus === 'running'}>
              {benchStatus === 'running' ? '评测中…' : '运行完整评测'}
            </button>
            <button
              className="btn ghost"
              onClick={() => benchReport && downloadJson('stepfun-benchmark.json', benchReport)}
              disabled={!benchReport}
            >
              下载评测 JSON
            </button>
          </div>
        }
      >
        {benchProgress ? (
          <div className="progress-box">
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${benchProgress.percent}%` }} />
            </div>
            <div className="progress-meta">
              <span>{benchProgress.step}</span>
              <span>{benchProgress.detail}</span>
            </div>
          </div>
        ) : null}
        {benchError ? <div className="inline-error">{benchError}</div> : null}

        {benchReport ? (
          <>
            <h3 className="table-title">
              检索质量与模型体积 · {benchReport.corpusDocs} 篇语料 / {benchReport.evalQueries} 条查询
            </h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>配置</th>
                    <th>模型体积</th>
                    <th>压缩比</th>
                    <th>向量化吞吐</th>
                    <th>混合 Recall@5</th>
                    <th>混合 nDCG@5</th>
                    <th>纯向量 Recall@5</th>
                    <th>纯向量 nDCG@5</th>
                  </tr>
                </thead>
                <tbody>
                  {benchReport.quality.map((row) => (
                    <tr key={row.dtype}>
                      <td>
                        {row.dtype === 'q8' ? 'INT8' : 'FP32'} + {row.device.toUpperCase()}
                      </td>
                      <td>{formatBytes(row.modelBytes)}</td>
                      <td>{row.compressionRatio.toFixed(2)}×</td>
                      <td>{row.docsPerSecond.toFixed(2)} 片段/秒</td>
                      <td>{(row.hybridRecall5 * 100).toFixed(1)}%</td>
                      <td>{row.hybridNdcg5.toFixed(3)}</td>
                      <td>{(row.vectorRecall5 * 100).toFixed(1)}%</td>
                      <td>{row.vectorNdcg5.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3 className="table-title">导入策略对比 · INT8 · {benchReport.perf[0]?.docs || 0} 篇</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>策略</th>
                    <th>总耗时</th>
                    <th>向量化</th>
                    <th>写入</th>
                    <th>索引刷新</th>
                    <th>吞吐</th>
                    <th>刷新次数</th>
                  </tr>
                </thead>
                <tbody>
                  {benchReport.perf.map((row) => (
                    <tr key={row.strategy}>
                      <td>{row.strategy === 'baseline' ? '未优化：逐条 + 每条刷新' : '优化：批量 8 + 单次刷新'}</td>
                      <td>{formatMs(row.totalMs)}</td>
                      <td>{formatMs(row.embedMs)}</td>
                      <td>{formatMs(row.writeMs)}</td>
                      <td>{formatMs(row.refreshMs)}</td>
                      <td>{row.docsPerSecond.toFixed(2)} 片段/秒</td>
                      <td>{row.refreshCount}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3 className="table-title">分阶段延迟（中位数）</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>配置</th>
                    <th>导入总耗时</th>
                    <th>查询向量化</th>
                    <th>seekdb 检索</th>
                  </tr>
                </thead>
                <tbody>
                  {benchReport.quality.map((row) => (
                    <tr key={`${row.dtype}-latency`}>
                      <td>
                        {row.dtype === 'q8' ? 'INT8' : 'FP32'} + {row.device.toUpperCase()}
                      </td>
                      <td>{formatMs(row.importMs)}</td>
                      <td>{formatMs(row.queryEmbedMsMedian)}</td>
                      <td>{formatMs(row.searchMsMedian)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="hint">
              评测时间：{new Date(benchReport.at).toLocaleString()} · 模型：{benchReport.model} · 体积按实际下载文件统计，
              FP32 为 INT8 的 {benchReport.quality[0]?.compressionRatio.toFixed(2)} 倍。
            </p>
          </>
        ) : (
          <p className="hint">
            尚未运行评测。点击“运行完整评测”后会依次加载 INT8 / FP32 模型、构建两个独立集合、执行固定查询集，
            并对比逐条写入与批量写入策略。首次运行需要下载约 24 MB 与 95 MB 的模型权重。
          </p>
        )}
      </Section>

      <footer className="footer">
        <span>模型：{MODEL}（ONNX 社区转换版） · 512 维 · CLS pooling + L2 normalize</span>
        <span>推理：Transformers.js 3.7.6 + ONNX Runtime Web（WebGPU / WASM）</span>
        <span>存储与检索：seekdb-js 1.4.0 嵌入式模式 · DBMS_HYBRID_SEARCH + RRF</span>
      </footer>

      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
