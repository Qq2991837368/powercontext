import type { Dtype, EmbedResult, ModelReadyInfo } from '../types';

type Pending = {
  resolve: (value: EmbedResult) => void;
  reject: (error: Error) => void;
  onProgress?: (done: number, total: number) => void;
};

export class EmbeddingClient {
  private worker: Worker;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private initResolve: ((info: ModelReadyInfo) => void) | null = null;
  private initReject: ((error: Error) => void) | null = null;
  private onInitProgress?: (progress: { file?: string; progress: number }) => void;

  constructor() {
    this.worker = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' });
    this.worker.onmessage = (event: MessageEvent) => this.handleMessage(event.data);
    this.worker.onerror = (event) => {
      const message = event.message || 'Worker 运行失败';
      this.initReject?.(new Error(message));
      for (const pending of this.pending.values()) pending.reject(new Error(message));
      this.pending.clear();
    };
  }

  private handleMessage(data: any) {
    if (data.type === 'progress') {
      this.onInitProgress?.(data);
      return;
    }
    if (data.type === 'ready') {
      this.initResolve?.(data as ModelReadyInfo);
      this.initResolve = null;
      this.initReject = null;
      return;
    }
    if (data.type === 'error') {
      if (data.scope === 'init') {
        this.initReject?.(new Error(data.message));
        this.initResolve = null;
        this.initReject = null;
      } else if (typeof data.requestId === 'number') {
        const pending = this.pending.get(data.requestId);
        pending?.reject(new Error(data.message));
        this.pending.delete(data.requestId);
      }
      return;
    }
    if (data.type === 'embed-progress') {
      this.pending.get(data.requestId)?.onProgress?.(data.done, data.total);
      return;
    }
    if (data.type === 'embedded') {
      const pending = this.pending.get(data.requestId);
      if (pending) {
        pending.resolve({
          vectors: data.vectors,
          ms: data.ms,
          docsPerSecond: data.docsPerSecond,
        });
        this.pending.delete(data.requestId);
      }
    }
  }

  init(options: {
    dtype: Dtype;
    device: 'webgpu' | 'wasm';
    model?: string;
    onProgress?: (progress: { file?: string; progress: number }) => void;
  }): Promise<ModelReadyInfo> {
    this.onInitProgress = options.onProgress;
    return new Promise<ModelReadyInfo>((resolve, reject) => {
      this.initResolve = resolve;
      this.initReject = reject;
      this.worker.postMessage({
        type: 'init',
        dtype: options.dtype,
        device: options.device,
        model: options.model,
      });
    });
  }

  embed(
    texts: string[],
    options: {
      batchSize?: number;
      onProgress?: (done: number, total: number) => void;
    } = {}
  ): Promise<EmbedResult> {
    const requestId = this.nextId++;
    return new Promise<EmbedResult>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject, onProgress: options.onProgress });
      this.worker.postMessage({
        type: 'embed',
        requestId,
        texts,
        batchSize: options.batchSize ?? 8,
      });
    });
  }

  terminate() {
    this.worker.terminate();
    this.pending.clear();
  }
}
