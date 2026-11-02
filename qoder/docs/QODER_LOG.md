# Qoder 开发记录

PaperLens 整体用 Qoder CLI 开发：需求拆解、代码生成、站点页面、发布与访问策略设置都在
Qoder 会话中完成；记录文件保存在 `records/qoder-sessions/*.jsonl`（`stream-json` 原始输出）。

## 1. 工具与环境

| 项 | 值 |
| --- | --- |
| Qoder CLI | `qodercli` 1.1.81 |
| 登录账号 | 已登录（`qodercli login` 浏览器授权） |
| 调用方式 | `qodercli -p "<目标>" -w <工程目录> --permission-mode bypass_permissions -o stream-json` |
| 模型 | Qoder 默认模型 |
| 站点发布 | Qoder Sites（`qoder sites prepare_site` / `publish_site`） |

## 2. 会话一览

| # | 会话文件 | 目标 | 轮次 | 耗时 | credits | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `01-base-layer.jsonl` | 配置、embedding、语料解析、依赖 | 14 | 681 s | 11.10 | 成功 |
| 2 | `02-store-retry.jsonl` | seekdb/PowerContext 存储层（重试） | 7 | 433 s | 7.37 | 成功 |
| 3 | `03-planner-retriever.jsonl` | 查询规划与混合检索 | 5 | 349 s | 3.50 | 成功 |
| 4 | `04-evidence-memory.jsonl` | 证据包与研究记忆折叠 | 5 | 280 s | 3.29 | 成功 |
| 5 | `05-cli-export.jsonl` | CLI 子命令与站点数据导出 | 5 | 302 s | 3.55 | 成功 |
| 6 | `06c-site-app.jsonl` | 单页站点（HTML/CSS/JS） | 7 | 183 s | 1.80 | 成功 |
| 7 | 会话 `55e3c1e8…` | 把 Qoder Site 访问权限改为 public | 4 | 73 s | 1.01 | 成功 |

合计约 31.63 credits。另有 `02-store.jsonl`、`06-site.jsonl`、`06b-site-app.jsonl`、
`07-publish.jsonl` 四段被中止的会话，原因见第 4 节。

## 3. 每个会话的产出

### 会话 1：基础层

提示词目标：按 `docs/SPEC.md` 实现配置、确定性本地 embedding、Markdown 语料解析与切块。

产出：

- `paperlens/config.py`：`Paths`、`PAPERLENS_STATE_DIR` 覆盖、scope 常量；
- `paperlens/embeddings.py`：384 维 `LocalHashEmbedder`；
- `paperlens/corpus.py`：front matter 解析、5 个小节校验、段落装箱；
- `paperlens/requirements.txt`、`__init__.py`、`__main__.py`。

人工复核：把 `MAX_CHUNK_CHARS` 从 420 调到 260，使块粒度更适合证据引用。

### 会话 2：存储层

提示词目标：用 PowerContext + seekdb 建立 scope，导入 `paper-meta` / `corpus-chunk` /
`corpus-manifest`，并提供 `search`、`create_note`、`list_notes`、`log_query`。

产出 `paperlens/store.py`。这一层遇到两次真实问题：

1. **manifest 超过 8192 UTF-8 字节**：首版把每个 chunk 的 `artifact_id` 都塞进 manifest，
   写入时抛 `ValueError`。第二次会话把 manifest 改为只保留论文级汇总与统计，并保留
   `manifest_artifact_id` 自引用。
2. **默认 admission 门槛过严**：PowerContext 默认 `AdmissionFloor` 要求语义相似度 >=0.3
   且词法覆盖达标，中文语料只召回 1 条。人工探针确认后改为显式
   `AdmissionFloor(lexical_coverage=0.0, lexical_min_matched_terms=1, min_semantic_similarity=0.0)`，
   让词法/向量任一路都能进入融合，排序仍由 RRF + 术语覆盖决定。

### 会话 3：规划与检索

提示词目标：领域术语表、跨语言扩展、最多 3 个查询变体；多路检索、RRF 融合、同小节去重、
每篇论文限量、证据句抽取。

产出 `paperlens/planner.py`、`paperlens/retriever.py`。

### 会话 4：证据与记忆

提示词目标：把命中结果组织成可回溯证据包（覆盖度、缺口、参考列表），并把
question / confirm / exclude / reading 四类事件折叠成研究记忆。

产出 `paperlens/evidence.py`、`paperlens/memory.py`。

### 会话 5：CLI 与导出

提示词目标：`ingest` / `ask` / `note` / `status` / `export-site` 五个子命令；把 6 个演示
问题离线跑成 `site/web/data/demo.json`。

产出 `paperlens/cli.py`、`paperlens/export.py`。人工修正：CLI 中 `scope_id` 改为读取
`store.scope_id`，避免导出数据出现空 scope。

### 会话 6：站点

提示词目标：中文浅色学术风单页，含 Hero 统计、痛点、工作流、演示切换、语料表、研究记忆、
运行记录、快速开始与边界说明；只读 `data/demo.json`。

产出 `site/web/index.html`、`site/web/assets/styles.css`、`site/web/assets/app.js`。
人工修正：

- `app.js` 证据卡重复渲染首句（同时保留了显式 `quotes[0]` 和 `quotesHtml`），删除重复行；
- 首屏快速开始命令的中文问题缺少引号，补成 `python -m paperlens ask "..."`。

### 会话 7：发布与访问策略

先用 `qoder sites prepare_site --input-file prepare-input.json` 准备，轮询到
`canPublish=true` 后 `publish_site` 成功。站点默认 `access_mode=private`，匿名访问返回 401；
在 Qoder 会话内调用 sites-management 能力把访问策略改为 `public`
（`confirmPublic=true`），随后匿名访问返回 200。

## 4. 被中止的会话与原因

Qoder CLI 会话在上下文较大时进入 `compacting` 后会长时间无输出。实际处理：

| 会话 | 现象 | 处理 |
| --- | --- | --- |
| `02-store.jsonl` | 写入 manifest 时超过 8192 字节，反复重试 | 另起精简会话 `02-store-retry` |
| `06-site.jsonl` | 生成整页时进入 `compacting`，进程 idle | 中止，拆成 `06b` |
| `06b-site-app.jsonl` | 同上 | 中止，缩小提示词后 `06c` 成功 |
| `07-publish.jsonl` | 发布步骤挂起 | 中止，改用 `qoder sites` CLI 直接发布 |

经验：把任务拆小、在提示词中给出明确文件清单和契约、限制工具集，可以显著降低
`compacting` 的概率；站点发布这类确定性操作直接调用 `qoder sites` 更快。

## 5. 人工修正清单

| 文件 | 问题 | 修正 |
| --- | --- | --- |
| `paperlens/store.py` | manifest 超 8192 字节 | 只保留论文级摘要 |
| `paperlens/store.py` | 默认 admission 只召回 1 条 | 显式放宽 `AdmissionFloor` |
| `paperlens/corpus.py` | 块过大（420 字符） | 上限调到 260，54 块 |
| `paperlens/cli.py` | 导出 scope 为空 | 使用 `store.scope_id` |
| `site/web/assets/app.js` | 证据句重复 | 删除显式首句渲染 |
| `site/web/index.html` | 快速开始命令缺引号 | 补双引号 |
| `tests/test_corpus.py` | 强行要求每块 >=160 字符 | 改为“正文非空且 <=260”，与段落切分契约一致 |
| `docs/SPEC.md` | 切块规则与实际不一致 | 同步为“段落优先、允许短块” |

## 6. 复现 Qoder 调用

```powershell
$prompt = "按 docs/SPEC.md 实现 paperlens/store.py，只写这一个文件，不要改动其他文件"
& qodercli -p $prompt `
  -w "D:\default-workspace\powercontext\qoder" `
  --permission-mode bypass_permissions -o stream-json
```

站点发布：

```powershell
& qodercli sites prepare_site --input-file records\qoder-sites\prepare-input.json
# 用返回的 actionId 轮询 get_publish_status，直至 canPublish=true
& qodercli sites publish_site --input-file <actionId 文件>
```

> `records/qoder-sites/prepare-input.json` 只含发布目录与站点名称，可随仓库提交；
> 轮询用的临时 actionId 文件不入库。
