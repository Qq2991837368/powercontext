# 技术说明（一页版）

## 1. 方案概览

应用分为两部分：浏览器端负责**文本分块与 embedding**，Node 端负责**写入 seekdb 与检索**。
浏览器通过 Web Worker 加载 `Xenova/bge-small-zh-v1.5`（ONNX 转换版，512 维，CLS pooling +
L2 normalize），将分块后的文本编码为向量；Node 端把 `{id, text, embedding, metadata}` 批量
`upsert` 到嵌入式 seekdb 集合。查询时浏览器只编码 query，把查询向量与原始关键词交给 seekdb，
由 `hybridSearch` 在库内完成向量 + 全文的 RRF 融合。**客户端不参与任何融合或重排**。

## 2. 推理后端与量化

- 模型资产：q8 ONNX 24,010,842 B，fp32 ONNX 94,851,877 B；含 tokenizer/config 的总文件体积
  分别为 24,451,175 B（23.32 MiB）与 95,292,210 B（90.88 MiB），压缩比 **3.90×**。
- 启动时显式探测 `navigator.gpu.requestAdapter()`；可用则优先 WebGPU，失败时捕获异常并降级
  WASM，UI 顶部横幅展示 `fallbackReason`。
- **关键实测结论**：在本机（RTX 3050 Ti + Chromium）上，q8 模型跑在 ORT WebGPU EP 时数值
  不可靠——固定 28 篇语料 / 13 条标注查询下，纯向量 Recall@5 从 WASM 的 **80.1%** 降到
  **21.8%**，吞吐也从 ~21 片段/秒降到 ~17 片段/秒。因此实现对 INT8 显式路由到 WASM 并给出
  可见提示；FP32 使用 WebGPU。这样既保留了 WebGPU 优先能力，也保证 INT8 检索质量。
- ORT 的 `.mjs` loader 不能从 Vite 的 `public/` 目录动态导入（Vite 会报 "should not be
  imported from source code"）。实现改为 `fetch` 该文件并生成 Blob URL，`.wasm` 使用同源
  绝对 URL，开发与生产构建行为一致。

## 3. seekdb 内的混合检索

集合 Schema 同时声明两种索引：

```js
fulltextIndex: new FulltextIndexConfig('ngram2', { min_ngram_size: 2, max_ngram_size: 3 })
vectorIndex: new VectorIndexConfig({
  hnsw: { dimension: 512, distance: 'cosine' },
  embeddingFunction: null,   // 向量由浏览器提供，禁用服务端 embedding
})
```

检索请求：

```js
collection.hybridSearch({
  query: { whereDocument: { $or: [{ $contains: term }, ...] }, nResults },
  knn: { queryEmbeddings: embedding, nResults },
  rank: { rrf: {} },
  nResults,
  include: ['documents', 'metadatas', 'distances'],
})
```

中文查询按空白/标点拆词，长 CJK 串按 3 字切块（最多 8 项），以 `$contains` 构造全文条件；
向量通道走 HNSW cosine。RRF 融合、排序、Top-N 截断全部发生在 seekdb 进程内。

## 4. 增量导入与索引刷新

导入流程：分块（默认 320 字 / 64 重叠）→ Worker 按 batch 8 向量化 → 前端按 16 条/批调用
`POST /documents`（`refresh=false`）→ 全部写完调用一次 `refresh_index()`。新增片段在刷新后
立即可检索；接口同时返回服务端实测的 `writeMs` / `refreshMs`。

嵌入式 seekdb 的原生引擎并发访问会导致 Windows 访问违例（`0xC0000005`），因此服务端用单一
Promise 队列串行化所有 `/api` 请求；响应很小，串行化不构成吞吐瓶颈。

## 5. 实测结果

环境：AMD Ryzen 7 6800H / RTX 3050 Ti Laptop，Chromium（WebGPU 可用），Node v24.19.0。

| 配置 | 模型体积 | 压缩比 | 向量化 | 混合 R@5 | 混合 nDCG@5 | 纯向量 R@5 |
| --- | --- | --- | --- | --- | --- | --- |
| INT8 (q8) + WASM | 23.32 MiB | 3.90× | 15.86 片段/秒 | 84.0% | 0.827 | 80.1% |
| FP32 + WebGPU | 90.88 MiB | — | 71.52 片段/秒 | 84.0% | 0.829 | 80.1% |

- 质量集：28 篇内置语料，13 条人工标注查询，指标为 Recall@5 / nDCG@5 的宏平均。
- 导入策略（INT8，12 篇）：逐条 + 每条刷新 4922 ms（12 次刷新）vs 批量 8 + 单次刷新
  1239 ms（1 次刷新），约 3.97×；索引刷新是主要成本（单次约 320–450 ms）。
- 查询延迟中位数（28 篇库）：查询向量化 15.5 ms（INT8/WASM）/ 12.0 ms（FP32/WebGPU），
  seekdb 检索 6.8 ms / 6.6 ms。
- 首次导入 56 个片段（WebGPU q8 实验路径）：向量化 3857 ms、写入 664 ms、刷新 443 ms，
  端到端 5350 ms。

## 6. 取舍与边界

- 体积口径统一按实际下载文件字节（含 tokenizer 与 config），不使用参数数量估算。
- 预置的 28 篇语料与 13 条查询用于可复现的质量对比；评测会重建独立集合并保留原始 JSON。
- INT8 与 FP32 各自建库，避免不同精度向量混用；切换精度时 UI 会提示并自动新建集合。
- 模型可由 `npm run fetch:model` 预下载，也可回退到 hf-mirror / Hugging Face 远程拉取，
  但推理始终在本地浏览器完成，不调用任何云端推理接口。
