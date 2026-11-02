# 一页技术说明：规有所依的 SDD 上下文管理

## 目标

把「规格、代码、测试」变成可检索、可治理、可复用的工程记忆，让 MiniMax 在每次开发任务中
拿到正确的上下文，并把任务进展与验证结果留在数据库里，供后续进程和后续规格版本复用。

## 架构与分工

```text
specs / src / tests
        │  init（导入）
        ▼
seekdb（嵌入式）── PowerContext Atomic Memory ── 全文索引 + 向量索引 + 语料 manifest
        │  context（hybrid 检索 + 精确读取当前源码 + 读取最新状态）
        ▼
开发上下文（Markdown / JSON，含 artifact id、score、matched_by）
        │
        ▼
MiniMax-M3.1-Flash（OpenAI 兼容 /v1/chat/completions，返回完整新文件）
        │
        ▼
pytest 目标测试 + 回归测试 ── solve 把验证结果写回 task-state
        │
        ▼
PowerContext 状态（seq 单调递增）── 新进程 status/context 读取并复用
```

- **MiniMax**：只负责阅读上下文与生成代码，不接触数据库、不直接跑测试。
- **seekdb**：持久化语料与索引；检索与版本过滤在数据库侧完成。
- **PowerContext 1.4.1**：提供 Scope、Atomic Memory、`create/list/get/search/forget`、
  mixed retrieval 与语料治理。
- **CLI**：负责编排、校验、证据落盘，保证每一步可审计。

## 数据模型

所有内容都是同一个 Scope 下的 Atomic Memory，按 `kind` 区分：

| kind | 内容 |
| --- | --- |
| `spec` / `code` / `test` | 每个文件一条，正文以 `[[SOURCE 相对路径]]` 开头 |
| `corpus-manifest` | JSON：语料版本、导入时间、文件 sha256、artifact id |
| `task-state` | JSON：seq、task、status、summary、verification、notes |

`task-state` 的 artifact id 是随机生成的，`list` 按 id 排序，因此「最新状态」不依赖列表顺序，
而是由状态自身的 `seq` 递增并在读取时取最大值决定；这也是跨进程稳定复用的关键。

## 检索与上下文

- `search(task, mode="hybrid", kind=...)`：同一条查询走全文与 256 维向量两个通道，
  返回分数与 `matched_by`，上下文里原样保留 artifact id 和检索证据。
- 本地 embedding 采用确定性的哈希词袋 + 字符二元组，固定 256 维；不依赖外部 embedding
  服务，保证离线可复现。更换维度会触发 PowerContext 的索引维度校验，需要重建数据库。
- 规格/测试片段由检索得到；**当前源码**则按 manifest 中的 artifact id 精确 `get`，避免
  模型拿不到待修改文件，也保证模型看到的源码与数据库记录一致。
- `solve` 在使用上下文前强校验 `corpus_version` 与当前 manifest 一致，防止旧规格残留。

## 持续演进

- `init --refresh`：先对旧 `spec/code/test/corpus-manifest` 逐条 `forget`，再写入新版本；
  检索只会看到新语料，同时 manifest 保留版本号。
- 规格 v2 新增 R5（`min_price`）后重新导入，任务 2 的上下文包含 R5 文本与新测试；
  上一次任务的 `done` 状态仍在历史中，被新上下文读取。

## 状态与验证

- 任务开始时写入 `planned`（verification 为空），验证完成后写入 `done`，失败则写入 `failed`。
- `verification` 记录修改前失败、修改后目标/回归结果、日志路径、会话路径、diff 与前后快照。
- `solve` 在模型输出不合法、语法错误、测试失败时恢复原文件，并把失败输出作为下一轮反馈；
  最多重试 2 次。任务 1 的第一轮因输出被截断被拒绝，第二轮通过，记录完整保留。

## 安全与边界

- 不修改测试与规格；两次 diff 均只触及 `src/book_filter.py`。
- 模型输出先做标记解析、`filter_books` 存在性检查与 `compile()` 语法检查，再落盘。
- API Key 只读环境变量，不写日志、不写会话记录、不提交仓库。
- 当前实现面向单 Scope、小规模工程（4–5 个文件）与单文件修改；多文件补丁、远程
  OceanBase、真实语义 embedding 可作为后续扩展，不影响本赛题的验收链路。
