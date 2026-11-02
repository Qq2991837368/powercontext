# PaperLens 验证样例

本文件记录 PaperLens 在真实环境中的运行命令与输出，所有结果均来自 2026-11-02 的实际执行，
不是手工构造的示例。

## 1. 验证环境

| 项 | 值 |
| --- | --- |
| 运行环境 | WSL2 `pc-lab`，Linux 原生文件系统 |
| Python | 3.12（`/root/pc-venv` 虚拟环境） |
| 依赖 | `powercontext[seekdb]==1.4.1`、`pytest>=8,<9` |
| 状态目录 | `PAPERLENS_STATE_DIR=/root/paperlens-run` |
| 代码位置 | `/mnt/d/default-workspace/powercontext/qoder` |
| 语料 | `corpus/papers/` 8 篇中文摘要级笔记，共 54 个文本块 |

> seekdb 只能打开 Linux 原生文件系统上的目录；把状态目录放在 `/mnt/d` 会失败，必须放在
> `/root` 下。

## 2. 从零复现

```bash
cd /mnt/d/default-workspace/powercontext/qoder
export PAPERLENS_STATE_DIR=/root/paperlens-demo

python -m paperlens ingest --refresh
python -m paperlens ask "HNSW 的 ef 参数如何影响召回率和查询延迟？应该先调 ef 还是先调 M？" --top 5
python -m paperlens note --kind question --text "内存受限条件下如何构建十亿级向量检索系统"
python -m paperlens note --kind confirm  --text "PQ 把向量压到 m 字节，适合作为内存中的导航层"
python -m paperlens note --kind exclude  --text "暂不考虑依赖 GPU 的方案"
python -m paperlens note --kind reading  --paper hnsw-2018 --status read
python -m paperlens status
python -m paperlens export-site
python -m pytest -q
```

## 3. 语料导入

```bash
$ python -m paperlens ingest --refresh
```

导入记录（`records/corpus-imports.jsonl`，真实输出）：

```json
{"at": "2026-11-02T16:26:03.744308+00:00", "chunk_total": 54,
 "corpus_fingerprint": "a9faaae978047c853c8047f6b8f8d6845ad26a5a6f3b6ebce8ba8e329ea47f49",
 "manifest_artifact_id": "art_1de0372a82d94f94a46d64aadbcf4917",
 "paper_total": 8, "refresh": true,
 "scope_id": "scp_2p012trver1cdhf9fmj9w4n0wd", "version": 1}
```

`manifest.version=1`、8 篇论文、54 个 chunk；同一份语料的指纹在重复解析时保持不变。

## 4. 证据包生成

查询：

> HNSW 的 ef 参数如何影响召回率和查询延迟？应该先调 ef 还是先调 M？

真实输出保存在 [`records/evidence/20261102T162754043Z.md`](../records/evidence/20261102T162754043Z.md)，
关键片段：

```text
## 检索计划
- 术语：HNSW、召回率、延迟
- 扩展：图索引、graph index、graph-based index、recall、时延、latency
- 变体：原问题 / 原问题 + 扩展 / 中文术语替换为英文，共 3 条

## 证据卡
### [P1] Efficient and Robust Approximate Nearest Neighbor Search ...
· 2018 · IEEE TPAMI · 方法与设计
- 命中理由：score=0.9500，matched_by=text、vector，terms=HNSW、召回率、延迟
> M 决定图的质量与内存占用，ef 决定召回率与延迟的权衡

### [P2] 同一论文 · 关键实验与结论
- 命中理由：score=0.9329，matched_by=text、vector
> 因此工程上常把 ef 设为返回条数 k 的数倍，在召回率与延迟之间折中

## 覆盖度
- 覆盖率：100.0%（3/3 个术语）
- 命中论文：4/8
- 未被命中的论文：diskann-2019、ivf-adc-2006、mrl-2022、scann-2020
```

验证要点：

- 每条证据都带论文、年份、venue、小节和原句，可回溯；
- `matched_by=text、vector` 说明词法与向量两路都贡献了候选；
- 覆盖率与未命中论文被显式列出，而不是假装“全部相关”。

## 5. 研究记忆与跨进程复用

写入 4 类研究记忆后，在**独立的 WSL 进程**中执行 `status`，仍能读回同一份状态：

```text
$ PAPERLENS_STATE_DIR=/root/paperlens-run python -m paperlens status
索引状态
  版本：1
  论文数：8
  文本块数：54
  语料指纹：a9faaae978047c853c8047f6b8f8d6845ad26a5a6f3b6ebce8ba8e329ea47f49
研究记忆
  当前问题：内存受限条件下如何构建十亿级向量检索系统
  已确认：1 条
  已排除：1 条
  排除论文：0 篇
  待读：3 条
最近查询
  - 乘积量化 PQ 怎样在有限内存下压缩向量？精度损失来自哪里，ADC 为什么仍然可用？  覆盖率=1.00
```

这说明研究记忆写在 seekdb 的 `research-note` 原子里，不依赖进程内内存。

## 6. 站点数据导出

```bash
$ python -m paperlens export-site
```

生成的 `site/web/data/demo.json` 为真实离线预计算结果：

```json
{
  "scope_id": "scp_2p012trver1cdhf9fmj9w4n0wd",
  "corpus": {"papers": 8, "chunks": 54, "manifest_version": 1,
             "fingerprint": "a9faaae978047c853c8047f6b8f8d6845ad26a5a6f3b6ebce8ba8e329ea47f49"},
  "cases": 6
}
```

站点本身只读这份 JSON，不在浏览器里运行 seekdb；这样发布出去的是可复核的证据快照，
而不是一个需要连接数据库的空壳页面。

## 7. 自动化测试

```text
$ pytest -q
...........                                                              [100%]
11 passed in 21.22s
```

覆盖内容：

| 用例 | 验证点 |
| --- | --- |
| `tests/test_corpus.py` | 8 篇论文、>=50 chunk、唯一 id、5 个小节、front matter、指纹顺序无关 |
| `tests/test_planner.py` | 术语识别、跨语言扩展、变体不超过 3 条、`find_terms` 顺序 |
| `tests/test_memory.py` | 非法事件拒绝、按 `seq` 折叠、去重、阅读状态覆盖 |
| `tests/test_store_integration.py` | 真实 seekdb 导入 → 检索 → 写入笔记 → 重新打开运行时读回 |

集成用例使用 `tmp_path` 下的全新数据库，不会污染 `records/state`。

## 8. 本地站点验证

```bash
python -m http.server 8765 -d site/web
```

浏览器实际检查结果：

- Hero 统计显示 8 篇论文 / 54 个文本块 / 6 个演示问题 / 0 个外部 API 依赖；
- 默认展示第一个演示问题，渲染 4 张证据卡，每张含出处与多条原句；
- 切换到第 4 个演示问题后，证据卡与覆盖度同步更新；
- 控制台无 JS 报错，`data/demo.json` 请求返回 200。

## 站点发布

通过 Qoder Sites 发布，访问权限已设为 public：

| 项 | 值 |
| --- | --- |
| 站点地址 | <https://paperlens-2yzqmsmjcjd.qoder.website> |
| 站点名称 | PaperLens |
| Project ID | `01a19d33-6823-7e47-bca1-83bbd9dfb494` |
| Site ID | `01a19d33-6826-73bb-9cf5-d7b33ba299ec` |
| 发布操作 ID | `01a19d33-d9f0-7d49-bf89-fba5a59e2dd3` |
| 发布状态 | `published=true`，`readiness=publication_completed` |
| 访问策略 | `mode=public`，`auth_integration_status=not_required` |
| 匿名访问 | HTTP 200，返回站点 HTML |

发布目录为 `qoder/site/web`（`webDirectory=web`），站点数据由 `export-site` 生成后
一起打包上传；页面不依赖本地服务，评审可直接打开。

## 9. 已知边界

- 语料为作者整理的中文摘要级笔记，不是论文原文；引用请以原论文为准。
- 本地 embedding 是确定性哈希词袋，语义泛化能力弱于真实 embedding；检索质量靠
  术语扩展与 RRF 重排补偿。
- 单条记忆文本上限 8192 UTF-8 字节，因此 `corpus-manifest` 只保存论文级摘要，
  块级信息落在 `paper-meta`。
- 站点展示的是导出时的快照；新增语料后需要重新 `export-site` 并重新发布。
