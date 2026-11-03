# StepFun × seekdb：浏览器本地向量化的迷你知识库

一个可运行的 Web 应用：文本在浏览器内完成分块与 embedding，向量与原文写入嵌入式
[seekdb](https://github.com/oceanbase/seekdb)，向量 + 全文混合检索（RRF）完全在 seekdb
内部完成，客户端不做任何二次融合。支持 INT8 / FP32 量化对比、WebGPU 优先与 WASM 自动降级、
增量导入与索引刷新。

## 功能对照

| 赛题要求 | 实现 |
| --- | --- |
| 浏览器本地 embedding | Transformers.js 3.7.6 + ONNX Runtime Web，`Xenova/bge-small-zh-v1.5`（512 维，CLS + L2 normalize），运行在 Web Worker |
| seekdb 内混合检索 | `Schema` 中同时声明 `ngram2` 全文索引与 HNSW(cosine) 向量索引，检索调用 `hybridSearch({ query, knn, rank: { rrf: {} } })` |
| 禁止客户端融合 | 服务端只把查询向量与关键词传给 seekdb，融合与排序由 seekdb 返回；前端只渲染结果 |
| INT8 / FP16 量化 | 提供 `q8`（INT8）与 `fp32` 两套权重，报告体积、压缩比、Recall@5 / nDCG@5 |
| WebGPU 优先 + 降级提示 | 启动时探测 `navigator.gpu`；FP32 走 WebGPU，失败自动降级 WASM；INT8 因 WebGPU EP 数值问题固定 WASM，页面顶部横幅说明原因 |
| 增量导入 + 索引刷新 | 批量 `upsert` → 单次 `refresh_index()`；导入完成即可检索新增内容 |
| 禁止云端推理 / 预计算向量 | 模型文件本地 `/models/` 托管，向量只在当前浏览器会话内产生，后端不做 embedding |

## 架构

```
┌──────────────────────────── 浏览器 ────────────────────────────┐
│ React UI                                                        │
│   └─ Web Worker: Transformers.js + ORT (WebGPU 或 WASM)          │
│        分块文本 → 512 维向量（本地生成，不离开设备）             │
└───────────────────────────────┬────────────────────────────────┘
                                │ HTTP（向量 + 原文）
┌───────────────────────────────▼────────────────────────────────┐
│ Node.js + Express（server/index.mjs）                           │
│   └─ seekdb-js 1.4.0 嵌入式（server/data/seekdb）                │
│        collection: document 字段 ngram2 全文 + 512d HNSW 向量    │
│        hybridSearch: DBMS_HYBRID_SEARCH + RRF（库内融合）        │
└────────────────────────────────────────────────────────────────┘
```

## 快速开始

环境：Node.js ≥ 20（实测 v24.19.0），npm ≥ 10。

```bash
cd stepfun
npm install
npm run fetch:model     # 从 hf-mirror 下载 q8 + fp32 权重与 tokenizer（约 119 MB）
npm run dev             # 后端 8787 + 前端 5173
# 打开 http://localhost:5173
```

生产构建：

```bash
npm run build           # tsc --noEmit && vite build → web/dist
npm start               # Express 同时提供 API 与静态页面，http://localhost:8787
```

模型文件在 `web/public/models/Xenova/bge-small-zh-v1.5/`（构建时原样拷贝）。
若本地模型缺失，Worker 会自动探测 `hf-mirror.com` / `huggingface.co`，仍保持浏览器端推理。
ORT 的 wasm 资产由 `vite.config.ts` 在启动/构建时从 `node_modules/onnxruntime-web/dist`
复制到 `web/public/ort/`，不依赖第三方 CDN。

## 目录

```
stepfun/
├─ server/index.mjs            # seekdb 集合管理 / 写入 / 刷新 / 检索 API
├─ web/src/embedding/worker.ts # 模型加载、WebGPU→WASM 路由、本地向量化
├─ web/src/embedding/client.ts # Worker 封装
├─ web/src/bench/runBenchmark.ts# 固定语料质量评测 + 导入策略对比
├─ web/src/data/corpus.ts      # 28 篇语料 + 13 条带标注查询 + 模型体积清单
├─ web/src/App.tsx             # 导入 / 检索 / 会话指标 / 评测面板
├─ scripts/fetch-model.mjs     # 模型下载
├─ scripts/smoke-test.mjs      # 后端全链路冒烟测试
└─ docs/TECH_NOTES.md, docs/VERIFICATION.md
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 引擎、数据库、集合数量、uptime |
| GET | `/api/kbs` | 知识库列表（集合名、文档数、精度、维度） |
| POST | `/api/kbs` | 创建集合（`ngram2` 全文 + 512d HNSW） |
| GET/DELETE | `/api/kbs/:id` | 详情 / 删除集合 |
| POST | `/api/kbs/:id/documents` | 批量 upsert，`refresh=true` 时写入后刷新索引 |
| POST | `/api/kbs/:id/refresh` | 显式刷新向量索引 |
| POST | `/api/kbs/:id/search` | `hybrid` / `vector` / `fulltext`，返回库内排序结果与耗时 |
| GET | `/api/kbs/:id/documents/:docId` | 读取单条原文 |

嵌入式 seekdb 通过单一 Promise 队列串行访问（原生引擎不支持并发请求），避免 Windows
下偶发的 `0xC0000005` 崩溃。

## 实测数据（RTX 3050 Ti Laptop + Chromium WebGPU / WASM）

| 配置 | 体积 | 压缩比 | 向量化吞吐 | 混合 Recall@5 | 混合 nDCG@5 | 纯向量 Recall@5 |
| --- | --- | --- | --- | --- | --- | --- |
| INT8 (q8) + WASM | 23.32 MiB | 3.90× | 15.86 片段/秒 | 84.0% | 0.827 | 80.1% |
| FP32 + WebGPU | 90.88 MiB | — | 71.52 片段/秒 | 84.0% | 0.829 | 80.1% |

导入策略（INT8，12 篇）：逐条写入 + 每条刷新 4922 ms（12 次刷新）；批大小 8 + 单次刷新
1239 ms（1 次刷新），约 3.97× 加速。查询阶段 seekdb 检索中位数约 6.8 ms。

> 实测发现：q8 在 ORT WebGPU EP 上数值不可靠（纯向量 Recall@5 降至 21.8%，吞吐也下降），
> 因此 INT8 自动使用 WASM，FP32 使用 WebGPU。详见 `docs/TECH_NOTES.md`。

## 文档

- 技术说明（Markdown）：[docs/TECH_NOTES.md](docs/TECH_NOTES.md)
- 技术说明（PDF，2 页）：[docs/TECH_NOTES.pdf](docs/TECH_NOTES.pdf)
- 复现步骤与测试日志：[docs/VERIFICATION.md](docs/VERIFICATION.md)
- 后端冒烟日志：[scripts/smoke-test.log](scripts/smoke-test.log)
