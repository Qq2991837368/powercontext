# PaperLens 开发规格（Qoder 实现契约）

PaperLens 是一个科研文献证据检索助手：把本地论文摘要语料导入 seekdb，基于 PowerContext
的 scope / atomic memory 做混合检索，输出带出处的证据包，并把研究过程中的问题、已确认结论、
排除方向与阅读状态持久化为“研究记忆”。

本文件是给 Qoder CLI 的实现契约。**不要臆造 PowerContext API**，只能使用第 5 节列出的签名。

## 1. 目录约定

```
qoder/
├── paperlens/               Python 包
├── corpus/
│   ├── papers/*.md          论文语料（含 YAML front matter）
│   └── demo-questions.json  站点演示问题
├── records/                 运行记录（git 保留结构，运行时生成内容）
├── site/                    Qoder Sites 的 projectRoot
│   └── web/                 webDirectory（公开输出，含 index.html）
└── docs/                    规格、验证、开发记录
```

包根 = `paperlens/` 的父目录。运行命令一律在 `qoder/` 下执行：`python -m paperlens ...`。

## 2. 运行环境

- Python 3.11+
- `powercontext[seekdb]==1.4.1`
- 仅用标准库 + powercontext，不引入第三方依赖（不需要 numpy/httpx/LLM SDK）
- seekdb 只能在 Linux 原生文件系统上打开：WSL 下须把 `PAPERLENS_STATE_DIR` 指到 `/root/...`，
  不能放在 `/mnt/d`

## 3. 配置（`paperlens/config.py`）

```python
PACKAGE_ROOT: Path          # paperlens 包的父目录
CORPUS_DIR / PAPERS_DIR / RECORDS_DIR / SITE_DIR / DEMO_QUESTIONS

@dataclass(frozen=True)
class Paths:
    root: Path
    papers: Path
    records: Path
    site: Path
    state: Path       # seekdb 数据库目录
    db: Path          # state / "seekdb"
    evidence: Path    # records / "evidence"
    @staticmethod
    def default() -> "Paths"
    def ensure(self) -> None   # mkdir 全部目录

def load_paths() -> Paths
def scope_key() -> str
```

环境变量：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `PAPERLENS_STATE_DIR` | seekdb 目录 | `<root>/records/state` |
| `PAPERLENS_SCOPE_KEY` | scope 幂等键 | `paperlens-demo-v1` |

`SCOPE_TITLE = "PaperLens 科研文献证据库"`，`SCOPE_SUMMARY = "ANN/向量检索方向的文献证据与研究记忆"`。

## 4. 语料格式

论文为 Markdown，front matter 在文件开头：

```markdown
---
id: hnsw-2018
title: ...
authors: ...
year: 2018
venue: ...
tags: [HNSW, 图索引]
---

## 1. 研究问题
正文...
```

解析规则：

- front matter 必须成功解析出 `id/title/authors/year/venue/tags`；缺失字段报错并指出文件
- 正文按行首 `## ` 切分为小节；小节标题去掉编号前缀 `1. ` 后作为 `section`
- 小节内部按空行切段落；以段落为最小切分单位，尽量把连续小段落装箱到不超过 260 字符；
  段落本身较小时允许块低于 160 字符（优先保持段落语义完整）；
  每个 chunk 文本以 `## {section}\n` 开头再拼接正文
- chunk id 形如 `{paper_id}#{section_slug}#{ordinal}`，`section_slug` 由标题取前 12 个字符并
  把空白替换为 `-`
- 每个 chunk 记录 `sha256`、字符数、段落在小节内的序号
- 语料整体计算 manifest 指纹

## 5. PowerContext 接口（唯一允许的调用方式）

```python
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.runtime.models import AtomicMemoryContent
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage

config = BuiltinConfig(database=SeekDBConfig(path=paths.db))
async with open_builtin_runtime(config, embedding_model=embedder) as runtime:
    scope = await runtime.scopes.create(
        ScopeDraft(title=SCOPE_TITLE, summary=SCOPE_SUMMARY, idempotency_key=scope_key())
    )
    memory = runtime.atomic_memory.for_scope(scope.scope_id)

    created = await memory.create((AtomicMemoryContent(kind=..., text=...),))
    artifact_id = created.records[0].ref.artifact_id

    page = await memory.list(kind=..., limit=100, cursor=None)
    # page.items -> record.ref.artifact_id / record.ref.revision /
    #               record.artifact.content.text / record.artifact.content.kind /
    #               record.state.state_version
    # page.next_cursor -> str | None

    record = await memory.get(artifact_id)

    result = await memory.search(query, mode="hybrid", kind=..., limit=30)
    # result.hits -> hit.hit.kind / hit.hit.score / hit.hit.artifact_ref.artifact_id
    #                hit.matched_by (tuple[str, ...]) / hit.text

    await memory.forget(artifact_id, expected_revision=..., expected_state_version=...)
```

embedding 实现需提供：

```python
class LocalHashEmbedder:
    profile = EmbeddingProfile(
        profile_id="local-hash-384", model="local-hash",
        dimension=384, distance="l2", normalization="unit",
    )
    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=..., usage=InferenceUsage(requests=1, input_tokens=...))
```

向量算法（确定性、离线）：小写化；切出 ASCII 单词、CJK 单字、CJK 二元组；对每个 token 用
`blake2b(..., digest_size=8)` 取模落桶，按 digest 字节奇偶取正负号；CJK 二元组权重 1.0、
CJK 单字权重 0.45、ASCII 单词权重 1.2；最后 L2 归一化。空文本返回全零向量。

**profile_id/dimension 改动会导致索引不兼容，必须重建库。**

## 6. 模块契约

### 6.1 `paperlens/corpus.py`

```python
@dataclass(frozen=True)
class PaperMeta:
    id: str; title: str; authors: str; year: int; venue: str; tags: tuple[str, ...]

@dataclass(frozen=True)
class Chunk:
    chunk_id: str; paper_id: str; section: str; ordinal: int
    text: str; sha256: str; char_count: int

@dataclass(frozen=True)
class Paper:
    meta: PaperMeta; path: str; sha256: str; chunks: tuple[Chunk, ...]

def load_papers(papers_dir: Path) -> tuple[Paper, ...]
def corpus_fingerprint(papers: tuple[Paper, ...]) -> str
```

### 6.2 `paperlens/store.py`

```python
class Store:
    runtime; scope_id; paths; memory

    async def import_corpus(self, *, refresh: bool) -> dict
        # 写 kind="paper-chunk"（每个 chunk 一条，文本首行为 "[[SOURCE {paper_id}]]"）
        # 写 kind="paper-meta"（每篇一条 JSON）
        # 写 kind="corpus-manifest"（一条 JSON，version 自增）
        # 注意：PowerContext 单条记忆文本上限 8192 UTF-8 字节，清单只存
        # {version, imported_at, scope_id, corpus_fingerprint, papers:[{id,path,sha256,chunk_count}],
        #  chunk_total, paper_total}，不要把每块 artifact_id 写进清单；块级信息存 paper-meta
        # refresh=True 时先 forget 掉 CHUNK_KIND 与 META_KIND（保留 MANIFEST_KIND 历史，
        # 使清单 version 持续递增）
        # 返回 manifest dict

    async def current_manifest(self) -> dict | None    # 取 version 最大的 manifest
    async def search(self, query: str, *, kind: str, limit: int) -> list[dict]
        # 必须显式传 admission 放宽准入阈值：
        #   from powercontext.builtin.artifacts.search import AdmissionFloor
        #   AdmissionFloor(lexical_coverage=0.0, lexical_min_matched_terms=1,
        #                  min_semantic_similarity=0.0)
        # 原因：本地确定性 embedding 的余弦相似度达不到默认 0.3，中文问题又难以满足
        # 默认词法覆盖门槛，不放宽就只有 1 条命中。放宽后 text/vector 两路都能出候选。
        # 调用 memory.search(query, mode="hybrid", kind=kind, limit=limit, admission=...)
        # 返回 {kind, source, artifact_id, score, matched_by, text}
    async def get_artifact(self, artifact_id: str) -> dict
    async def create_note(self, payload: dict) -> str  # kind="research-note"
    async def list_notes(self) -> list[dict]           # 解析 JSON 并按 seq 升序
    async def log_query(self, payload: dict) -> str    # kind="query-log"
    async def list_query_logs(self, *, limit: int) -> list[dict]

async def open_store(paths: Paths, *, reset: bool = False) -> AsyncIterator[Store]
```

所有读写都追加到 `records/db-queries.jsonl`（字段：`at/operation/kind/...`），作为真实运行记录。
`import_corpus` 同时追加 `records/corpus-imports.jsonl`。

### 6.3 `paperlens/planner.py`

```python
@dataclass(frozen=True)
class QueryPlan:
    question: str
    terms: tuple[str, ...]        # 原问题切出的检索词
    expansions: tuple[str, ...]   # 命中的领域词扩展
    variants: tuple[str, ...]     # 用于检索的 1~3 个查询串

def plan_query(question: str) -> QueryPlan
def find_terms(text: str, terms: Iterable[str]) -> tuple[str, ...]
```

- 内置中英术语表（至少覆盖语料主题）：召回率/recall、延迟/latency、量化/quantization、
  乘积量化/PQ、倒排/inverted file/IVF、图索引/graph/HNSW、十亿级/billion-scale、内存/memory、
  磁盘/SSD/disk、各向异性/anisotropic、内积/inner product/MIPS、残差/residual、精排/rerank、
  粗筛/coarse、维度/dimension、码本/codebook、子空间/subspace、邻居/neighbor、构建/construction、
  自适应/adaptive、嵌套/Matryoshka、压缩/compression
- `variants` 至少包含原问题；若命中扩展词，追加一条“原问题 + 扩展英文词”的查询串；
  中文问题再追加一条把中文术语替换为英文的查询串（去重后最多 3 条）

### 6.4 `paperlens/retriever.py`

```python
@dataclass(frozen=True)
class Hit:
    chunk_id: str; paper_id: str; section: str; text: str
    artifact_id: str; score: float; rrf: float; matched_by: tuple[str, ...]
    terms: tuple[str, ...]; evidence: tuple[str, ...]

async def retrieve(store, plan: QueryPlan, *, top: int, per_paper: int = 2,
                   exclude_papers: Iterable[str] = ()) -> list[Hit]
```

流程：

1. 对每个 variant 调 `store.search(..., kind="paper-chunk", limit=40)`
2. 以 `artifact_id` 做 RRF 融合，`k=60`
3. 解析 `[[SOURCE {paper_id}]]` 首行得到 paper_id
4. 同小节只保留分数最高的 chunk；每篇论文最多 `per_paper` 条；过滤 `exclude_papers`
5. 重排分 = `0.65 * rrf_norm + 0.30 * term_coverage + 0.05 * heading_bonus`，保留 4 位小数
6. `evidence` = 小节内包含任一检索词的句子（最多 3 句，按词覆盖数降序，句子长度 20–220 字符）

### 6.5 `paperlens/evidence.py`

```python
def build_pack(question, plan, hits, *, papers, memory_state, manifest, scope_id) -> dict
def render_markdown(pack: dict) -> str
def save_pack(paths, pack) -> Path       # records/evidence/<stamp>.json + .md
```

证据包字段：

```json
{
  "question": "...", "generated_at": "...", "scope_id": "...",
  "manifest": {"version": 2, "corpus_fingerprint": "...", "chunks": 87},
  "plan": {"terms": [], "expansions": [], "variants": []},
  "hits": [{"rank": 1, "paper": {"id","title","authors","year","venue"},
            "section": "...", "chunk_id": "...", "score": 0.0, "rrf": 0.0,
            "matched_by": [], "terms": [], "snippet": "...", "evidence": []}],
  "coverage": {"terms_total": 0, "terms_covered": [], "terms_missing": [],
               "ratio": 0.0, "papers_hit": [], "papers_total": 0},
  "gaps": {"uncovered_papers": [], "missing_terms": [], "note": "..."},
  "memory": {"question": "...", "confirmed": [], "excluded": [], "reading": {}}
}
```

Markdown 渲染要求：标题、生成时间、检索计划、按 `[P1]`…编号的证据卡（含出处行
`论文标题 · 年份 · venue · 小节`，引用片段用 `>` 引用块）、覆盖度与缺口、研究记忆、参考列表。

### 6.6 `paperlens/memory.py`

```python
def apply_note(notes, *, kind, text=None, paper=None, status=None) -> dict
def fold_notes(notes) -> dict
```

`research-note` 的 JSON：`{"seq": n, "type": "question|confirm|exclude|reading",
"text": "...", "paper": "hnsw-2018|null", "status": "read|skim|todo|null", "created_at": "..."}`。

折叠规则：`question` 取最后一条；`confirm`/`exclude` 累积去重；`reading` 按 paper 取最后状态。

### 6.7 `paperlens/cli.py`

```
python -m paperlens ingest [--refresh] [--json]
python -m paperlens ask "问题" [--top 6] [--json] [--no-save]
python -m paperlens note --kind question|confirm|exclude|reading --text "..." [--paper ID] [--status read|skim|todo]
python -m paperlens status [--json]
python -m paperlens export-site [--out site/web/data/demo.json]
```

- `ask` 先读 manifest 与 notes，构造证据包，保存到 `records/evidence/`，终端输出 Markdown footer
  提示保存路径
- `ask` 默认应用研究记忆：`exclude` 中的论文被过滤，`confirmed` 写入证据包 `memory`
- `status` 输出语料统计（论文数/chunk 数/manifest 版本）、研究记忆、最近 5 条 query-log 摘要
- 所有子命令在库为空且未 ingest 时给出明确中文提示
- 退出码：成功 0，用法错误 2，运行错误 1

### 6.8 `paperlens/export.py`

```python
async def export_site(store, paths, *, out: Path, top: int = 6) -> dict
```

读取 `corpus/demo-questions.json`（`[{"id","title","question","top"}]`），对每题跑一次
`plan_query` + `retrieve` + `build_pack`，把结果写成一个 `demo.json`：

```json
{
  "generated_at": "...", "scope_id": "...",
  "corpus": {"papers": 7, "chunks": 87, "fingerprint": "...", "manifest_version": 2,
             "paper_list": [{"id","title","authors","year","venue","tags","chunks"}]},
  "memory": {...},
  "cases": [{"id","title","question","pack": {...}}],
  "records": {"db_queries": 0, "evidence_packs": 0, "qoder_sessions": 0}
}
```

同时把证据包 Markdown 写入 `records/evidence/`，并把 demo.json 复制到 `site/web/data/demo.json`。

## 7. 站点契约

`site/web/index.html` 为 Qoder Sites 的入口页，纯静态，数据来自 `site/web/data/demo.json`，无 CDN、无网络请求。
页面包含：项目定位、工作流（SVG 图）、“问题 → 证据卡 → 覆盖度/缺口”交互演示、语料库列表、
真实运行记录、快速开始（命令）、技术栈与页脚。样式放 `site/web/assets/styles.css`，脚本放
`site/web/assets/app.js`。

## 8. 验收

- `python -m paperlens ingest` 后 `status` 显示 8 篇论文、chunk 数 >= 50
- `ask` 的证据包中每条 hit 都能追溯到 `paper_id + section`，`coverage.ratio` 在 0–1
- 两次独立进程运行 `ask` 得到相同的 `manifest.version` 与 `scope_id`（跨进程持久化）
- `export-site` 生成的 `demo.json` 可被站点离线渲染
- 全流程只依赖 seekdb + PowerContext，无外部 API Key
