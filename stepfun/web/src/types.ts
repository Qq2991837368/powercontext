export type Dtype = 'q8' | 'fp32';
export type DeviceKind = 'webgpu' | 'wasm';
export type SearchMode = 'hybrid' | 'vector' | 'fulltext';

export interface KbInfo {
  id: string;
  name: string;
  dtype: string;
  model: string | null;
  createdAt: string | null;
  count: number;
  dimension: number;
  distance: string;
}

export interface SearchHit {
  id: string;
  text: string;
  metadata: Record<string, unknown> | null;
  distance: number | null;
}

export interface SearchResponse {
  ok: boolean;
  mode: SearchMode;
  terms: string[];
  searchMs: number;
  results: SearchHit[];
}

export interface HealthInfo {
  ok: boolean;
  engine: string;
  mode: string;
  path: string;
  database: string;
  vectorDim: number;
  collectionCount: number;
  uptimeMs: number;
}

export interface ModelFileSize {
  name: string;
  bytes: number;
}

export interface ModelReadyInfo {
  dtype: Dtype;
  device: DeviceKind;
  requestedDevice: DeviceKind;
  actualDevice: DeviceKind;
  fallbackReason: string | null;
  loadMs: number;
  warmupMs: number;
  host: string;
  localModel: boolean;
  modelBytes: number;
  files: ModelFileSize[];
  fromResourceTiming: boolean;
}

export interface EmbedResult {
  vectors: number[][];
  ms: number;
  docsPerSecond: number;
}

export interface SessionImportMetrics {
  at: string;
  dtype: Dtype;
  device: DeviceKind;
  docs: number;
  chunks: number;
  chars: number;
  embedMs: number;
  writeMs: number;
  refreshMs: number;
  totalMs: number;
  docsPerSecond: number;
}

export interface QueryTiming {
  at: string;
  embedMs: number;
  searchMs: number;
  e2eMs: number;
}

export interface BenchmarkRow {
  dtype: Dtype;
  device: DeviceKind;
  modelBytes: number;
  compressionRatio: number;
  importMs: number;
  embedMs: number;
  writeMs: number;
  refreshMs: number;
  docsPerSecond: number;
  hybridRecall5: number;
  hybridNdcg5: number;
  vectorRecall5: number;
  vectorNdcg5: number;
  queryEmbedMsMedian: number;
  searchMsMedian: number;
}

export interface PerfRow {
  strategy: 'baseline' | 'optimized';
  docs: number;
  embedMs: number;
  writeMs: number;
  refreshMs: number;
  totalMs: number;
  docsPerSecond: number;
  refreshCount: number;
  addRequests: number;
}

export interface BenchmarkReport {
  at: string;
  corpusDocs: number;
  evalQueries: number;
  model: string;
  quality: BenchmarkRow[];
  perf: PerfRow[];
  raw: {
    quality: Record<string, unknown>[];
    perfQueries: Record<string, unknown>[];
  };
}
