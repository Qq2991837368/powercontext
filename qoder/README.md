# PaperLens — 科研文献证据检索助手

把文献检索变成可追溯的证据包。导入论文摘要语料，用 **seekdb** 的混合检索找证据，
用 **PowerContext** 记住你的研究判断（研究问题、已确认结论、排除方向、阅读状态）。

- 每一条结论都带出处：论文 / 年份 / venue / 小节 / 原句
- 每次查询都算覆盖度与缺口：哪些检索词没被证据覆盖、哪些论文没有命中
- 同一个库可跨进程复用研究记忆，不需要外部 API Key、不需要联网推理

## 1. 运行环境

- Python 3.11+
- `powercontext[seekdb]==1.4.1`（含 seekdb 嵌入式运行时）
- seekdb 只能打开 Linux 原生文件系统上的目录。**在 WSL 中运行，并把状态目录指到 `/root` 下**，
  不要放在 `/mnt/d`（drvfs 上无法打开数据库）。

```bash
cd qoder
python3 -m venv .venv          # 首次
. .venv/bin/activate           # 或直接用已有的虚拟环境
pip install -r requirements.txt
```

## 2. 三分钟跑通

```bash
export PAPERLENS_STATE_DIR=/root/paperlens-state   # 必须落在 Linux 原生盘
python -m paperlens ingest --refresh
python -m paperlens ask "HNSW 的 ef 参数如何影响召回率和查询延迟？应该先调 ef 还是先调 M？" --top 5
python -m paperlens status
python -m paperlens export-site
python -m http.server 8765 -d site/web             # 本地预览站点
```

`ask` 会把证据包写入 `records/evidence/`（`.json` 与 `.md`），并在每条命中上标注
`matched_by=text`、`matched_by=vector` 或两者，便于判断是哪一路检索贡献了候选。

## 3. 命令

| 命令 | 作用 |
| --- | --- |
| `python -m paperlens ingest [--refresh] [--json]` | 解析 `corpus/papers/`，写 seekdb 索引与语料清单 |
| `python -m paperlens ask "问题" [--top N] [--json] [--no-save]` | 生成证据包（Markdown / JSON） |
| `python -m paperlens note --kind question\|confirm\|exclude\|reading ...` | 写入研究记忆 |
| `python -m paperlens status [--json]` | 语料统计 + 研究记忆 + 最近查询 |
| `python -m paperlens export-site [--out PATH]` | 生成站点数据 `site/web/data/demo.json` |

研究记忆示例：

```bash
python -m paperlens note --kind question --text "内存受限条件下如何构建十亿级向量检索系统"
python -m paperlens note --kind confirm  --text "PQ 把向量压到 m 字节，适合作为内存中的导航层"
python -m paperlens note --kind exclude  --text "暂不考虑依赖 GPU 的方案"
python -m paperlens note --kind reading  --paper hnsw-2018 --status read
```

`exclude` 带 `--paper` 时，该论文会在后续检索中被过滤；不带 `--paper` 时只作为方向性判断保留。

## 4. 目录

```
qoder/
├── paperlens/          解析、索引、检索、证据包、研究记忆、CLI
├── corpus/papers/      8 篇 ANN / 向量检索方向的演示语料
├── records/            真实运行记录：证据包、查询日志、语料导入、Qoder 会话
├── site/               Qoder Sites 项目（web/ 为发布目录）
├── docs/               规格、验证样例、Qoder 开发记录
└── tests/              可离线运行的验证用例
```

## 5. 技术实现

- **存储与检索**：PowerContext 内置 seekdb 运行时。`paper-chunk` 每块一条原子记忆，
  `paper-meta` 保存论文元数据，`corpus-manifest` 保存版本化清单；检索走 `hybrid`（词法 + 向量）。
- **中文检索的准入阈值**：本地 embedding 是 384 维确定性哈希（blake2b 落桶 + CJK 二元组），
  余弦相似度达不到 PowerContext 默认的 0.3，中文长句也难以满足默认词法覆盖门槛。
  因此检索显式使用 `AdmissionFloor(lexical_coverage=0.0, lexical_min_matched_terms=1,
  min_semantic_similarity=0.0)`，让 text / vector 两路都能贡献候选，再用 RRF 融合并重排。
- **多查询变体**：原问题、原问题 + 中英同义扩展、中文术语替换为英文，三路检索后按
  `artifact_id` 做 RRF（k=60），同小节去重、每篇论文限量，最后用
  `0.65 * 归一化 RRF + 0.30 * 术语覆盖 + 0.05 * 小节命中` 重排。
- **研究记忆**：`research-note` 记录 `question/confirm/exclude/reading` 四类事件，
  按 `seq` 折叠成当前状态，跨进程从 seekdb 读回。
- **无外部依赖**：全流程不调用 LLM 或外部 API，证据句抽取与覆盖度计算都是确定性算法，
  同样的输入可复现同样的输出。

## 6. 数据说明

`corpus/papers/` 是作者根据公开论文整理的中文摘要级笔记（含 YAML front matter），
用于演示检索与证据组织，**不是论文原文**，引用时请以原论文为准。

## 7. 验证

见 [docs/VERIFICATION.md](docs/VERIFICATION.md)：包含真实运行命令与输出、自动化测试结果，
以及站点发布结果。`tests/` 下的用例可在离线环境重复执行：

```bash
pytest -q
```

## 8. 在线站点

Qoder Sites 发布地址见 [docs/VERIFICATION.md](docs/VERIFICATION.md#站点发布)。
站点数据由本地 Python 核心离线预计算（`export-site`），页面本身不运行 seekdb。

## 9. Qoder 开发记录

见 [docs/QODER_LOG.md](docs/QODER_LOG.md)：逐会话列出 Qoder CLI 的提示词目标、产出文件、
耗时与 Credits，以及人工复核后修正的问题。
