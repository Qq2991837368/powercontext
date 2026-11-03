import { api } from '../api';
import { CORPUS, EVAL_QUERIES, MODEL_MANIFEST } from '../data/corpus';
import type { EmbeddingClient } from '../embedding/client';
import { mean, median, ndcgAtK, recallAtK } from '../lib/metrics';
import type {
  BenchmarkReport,
  BenchmarkRow,
  Dtype,
  ModelReadyInfo,
  PerfRow,
} from '../types';

export interface BenchProgress {
  step: string;
  detail: string;
  percent: number;
}

interface ModelHandle {
  client: EmbeddingClient;
  info: ModelReadyInfo;
}

interface BenchOptions {
  ensureModel: (dtype: Dtype) => Promise<ModelHandle>;
  onProgress: (progress: BenchProgress) => void;
}

const K = 5;
const PERF_DOCS = 12;

async function recreateCollection(label: string, dtype: Dtype) {
  const kbs = await api.listKbs();
  for (const kb of kbs.filter((item) => item.name.startsWith('评测语料') || item.name.startsWith('性能对比'))) {
    await api.deleteKb(kb.id);
  }
  return api.createKb(label, dtype, MODEL_MANIFEST.model);
}

function docPayload(id: string, text: string, embedding: number[]) {
  return {
    id,
    text,
    embedding,
    metadata: { source: '内置评测语料', title: id },
  };
}

export async function runBenchmark({ ensureModel, onProgress }: BenchOptions): Promise<BenchmarkReport> {
  const quality: BenchmarkRow[] = [];
  const perf: PerfRow[] = [];
  const rawQuality: Record<string, unknown>[] = [];
  const rawPerf: Record<string, unknown>[] = [];

  const report: BenchmarkReport = {
    at: new Date().toISOString(),
    corpusDocs: CORPUS.length,
    evalQueries: EVAL_QUERIES.length,
    model: MODEL_MANIFEST.model,
    quality,
    perf,
    raw: { quality: rawQuality, perfQueries: rawPerf },
  };

  for (const dtype of ['q8', 'fp32'] as Dtype[]) {
    onProgress({
      step: `加载 ${dtype === 'q8' ? 'INT8' : 'FP32'} 模型`,
      detail: dtype === 'q8' ? '量化权重约 24 MB' : 'FP32 权重约 95 MB，首次需要下载',
      percent: dtype === 'q8' ? 8 : 30,
    });
    const { client, info } = await ensureModel(dtype);

    const kb = await recreateCollection(`评测语料 ${dtype.toUpperCase()}`, dtype);

    onProgress({
      step: '向量化评测语料',
      detail: `${CORPUS.length} 篇文档 · ${dtype.toUpperCase()} · ${info.device.toUpperCase()}`,
      percent: dtype === 'q8' ? 15 : 38,
    });
    const embedded = await client.embed(
      CORPUS.map((doc) => doc.text),
      { batchSize: 8 }
    );

    const docs = CORPUS.map((doc, index) => docPayload(doc.id, doc.text, embedded.vectors[index]));
    const addResult = await api.addDocuments(kb.id, docs, true);
    const importMs = embedded.ms + addResult.writeMs + (addResult.refreshMs || 0);

    onProgress({
      step: '检索质量评测',
      detail: `${EVAL_QUERIES.length} 条查询 · Recall@${K} / nDCG@${K}`,
      percent: dtype === 'q8' ? 24 : 46,
    });

    const hybridRecall: number[] = [];
    const hybridNdcg: number[] = [];
    const vectorRecall: number[] = [];
    const vectorNdcg: number[] = [];
    const queryEmbedTimes: number[] = [];
    const searchTimes: number[] = [];

    for (const [index, evalQuery] of EVAL_QUERIES.entries()) {
      const q = await client.embed([evalQuery.query], { batchSize: 1 });
      queryEmbedTimes.push(q.ms);

      const hybrid = await api.search(kb.id, {
        query: evalQuery.query,
        embedding: q.vectors[0],
        mode: 'hybrid',
        nResults: K,
      });
      const vector = await api.search(kb.id, {
        query: evalQuery.query,
        embedding: q.vectors[0],
        mode: 'vector',
        nResults: K,
      });
      searchTimes.push(hybrid.searchMs, vector.searchMs);

      const hybridIds = hybrid.results.map((hit) => hit.id);
      const vectorIds = vector.results.map((hit) => hit.id);
      const hRecall = recallAtK(hybridIds, evalQuery.relevant, K);
      const hNdcg = ndcgAtK(hybridIds, evalQuery.relevant, K);
      const vRecall = recallAtK(vectorIds, evalQuery.relevant, K);
      const vNdcg = ndcgAtK(vectorIds, evalQuery.relevant, K);
      hybridRecall.push(hRecall);
      hybridNdcg.push(hNdcg);
      vectorRecall.push(vRecall);
      vectorNdcg.push(vNdcg);

      rawQuality.push({
        dtype,
        device: info.device,
        queryIndex: index,
        query: evalQuery.query,
        relevant: evalQuery.relevant,
        hybridIds,
        vectorIds,
        hybridRecall: hRecall,
        hybridNdcg: hNdcg,
        vectorRecall: vRecall,
        vectorNdcg: vNdcg,
        queryEmbedMs: q.ms,
        hybridSearchMs: hybrid.searchMs,
        vectorSearchMs: vector.searchMs,
      });
    }

    // Use the fixed manifest (real bytes of the downloaded files). Resource
    // timing is cache-dependent and would under-report on a warm cache.
    const measuredBytes = MODEL_MANIFEST.dtype[dtype].total;
    quality.push({
      dtype,
      device: info.device,
      modelBytes: measuredBytes,
      compressionRatio: MODEL_MANIFEST.dtype.fp32.total / MODEL_MANIFEST.dtype.q8.total,
      importMs,
      embedMs: embedded.ms,
      writeMs: addResult.writeMs,
      refreshMs: addResult.refreshMs || 0,
      docsPerSecond: embedded.docsPerSecond,
      hybridRecall5: mean(hybridRecall),
      hybridNdcg5: mean(hybridNdcg),
      vectorRecall5: mean(vectorRecall),
      vectorNdcg5: mean(vectorNdcg),
      queryEmbedMsMedian: median(queryEmbedTimes),
      searchMsMedian: median(searchTimes),
    });
  }

  // ---- Same-condition optimization comparison (INT8 only, 12 docs) ----
  onProgress({ step: '性能对比：加载 INT8 模型', detail: '固定精度与设备，只改变批处理与写入/刷新策略', percent: 62 });
  const { client: q8Client, info: q8Info } = await ensureModel('q8');
  const perfDocs = CORPUS.slice(0, PERF_DOCS);

  onProgress({ step: '性能对比：未优化基线', detail: '逐条向量化 + 逐条写入 + 每条刷新索引', percent: 68 });
  const baselineKb = await recreateCollection('性能对比 基线', 'q8');
  let baselineEmbed = 0;
  let baselineWrite = 0;
  let baselineRefresh = 0;
  const baselineStart = performance.now();
  for (const [index, doc] of perfDocs.entries()) {
    const single = await q8Client.embed([doc.text], { batchSize: 1 });
    baselineEmbed += single.ms;
    const add = await api.addDocuments(
      baselineKb.id,
      [docPayload(`perf_base_${index}`, doc.text, single.vectors[0])],
      false
    );
    baselineWrite += add.writeMs;
    const refresh = await api.refresh(baselineKb.id);
    baselineRefresh += refresh.refreshMs;
  }
  const baselineTotal = performance.now() - baselineStart;
  perf.push({
    strategy: 'baseline',
    docs: perfDocs.length,
    embedMs: baselineEmbed,
    writeMs: baselineWrite,
    refreshMs: baselineRefresh,
    totalMs: baselineTotal,
    docsPerSecond: perfDocs.length / (baselineTotal / 1000),
    refreshCount: perfDocs.length,
    addRequests: perfDocs.length,
  });
  rawPerf.push({
    strategy: 'baseline',
    perDoc: perfDocs.map((doc) => ({ id: doc.id, chars: doc.text.length })),
    embedMs: baselineEmbed,
    writeMs: baselineWrite,
    refreshMs: baselineRefresh,
    totalMs: baselineTotal,
  });

  onProgress({ step: '性能对比：优化后', detail: '批大小 8 + 单次批量写入 + 导入结束刷新一次', percent: 76 });
  const optimizedKb = await recreateCollection('性能对比 优化', 'q8');
  const optimizedStart = performance.now();
  const optimizedEmbedded = await q8Client.embed(
    perfDocs.map((doc) => doc.text),
    { batchSize: 8 }
  );
  const optimizedDocs = perfDocs.map((doc, index) =>
    docPayload(`perf_opt_${index}`, doc.text, optimizedEmbedded.vectors[index])
  );
  const optimizedAdd = await api.addDocuments(optimizedKb.id, optimizedDocs, true);
  const optimizedTotal = performance.now() - optimizedStart;
  perf.push({
    strategy: 'optimized',
    docs: perfDocs.length,
    embedMs: optimizedEmbedded.ms,
    writeMs: optimizedAdd.writeMs,
    refreshMs: optimizedAdd.refreshMs || 0,
    totalMs: optimizedTotal,
    docsPerSecond: perfDocs.length / (optimizedTotal / 1000),
    refreshCount: 1,
    addRequests: 1,
  });
  rawPerf.push({
    strategy: 'optimized',
    corpus: perfDocs.map((doc) => doc.id),
    embedMs: optimizedEmbedded.ms,
    writeMs: optimizedAdd.writeMs,
    refreshMs: optimizedAdd.refreshMs || 0,
    totalMs: optimizedTotal,
    modelInfo: { device: q8Info.device, dtype: q8Info.dtype, modelBytes: q8Info.modelBytes },
  });

  onProgress({ step: '评测完成', detail: '结果已保存，可下载 JSON 原始数据', percent: 100 });
  return report;
}
