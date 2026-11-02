# 验收对照与真实验证结果

验证环境：

- WSL2 Ubuntu 24.04（内核由 Windows 主机提供），Python 3.12.3
- `powercontext[seekdb]==1.4.1` + `pylibseekdb`
- MiniMax 模型：`MiniMax-M3.1-Flash`（OpenAI 兼容接口）
- seekdb 数据库目录：`/root/sdd-demo-state/seekdb`
- PowerContext Scope：`scp_41q2enpf0v584j6y2w30b1r2e8`
- 语料：v1（4 个文件）→ v2（5 个文件，新增规格 R5 与 `tests/test_target_price_range.py`）

## 1. 能够运行

`python -m sdd_context init/context/record/status/solve` 均已真实执行，原始输出保存在：

- `records/corpus-imports.jsonl`：两次导入（v1、v2）
- `records/contexts/`：任务 1/2 的完整上下文与提示词
- `records/test-runs/`：全部 pytest 日志
- `records/state-events.jsonl`：四次状态写入

## 2. MiniMax 实际参与，且开发记录与代码差异对应

| 任务 | 会话文件 | 结果 | Token 用量 |
| --- | --- | --- | --- |
| 任务 1 尝试 1 | `records/minimax-sessions/20261102T145930828226Z-attempt1.json` | `finish_reason=length`，输出被截断，拒绝 | total 5741 / completion 4096 |
| 任务 1 尝试 2 | `records/minimax-sessions/20261102T145930828226Z-attempt2.json` | `finish_reason=stop`，通过 | total 4685 / completion 3011 / reasoning 2361 |
| 任务 2 尝试 1 | `records/minimax-sessions/20261102T150240632353Z-attempt1.json` | `finish_reason=stop`，通过 | total 3234 / completion 1072 / reasoning 330 |

模型响应 id：任务 1 两轮分别为 `0737d85327ae9e5a72c255db88c69168`（截断）与
`0737d86b2e00f7789bfeaf6e8e429512`（通过），任务 2 为 `0737d911fa8278d90e148672c83752d8`。

`records/session-integrity.log` 逐条校验了「会话中 `<<<FILE ...>>>` 内的完整代码」与
`records/snapshots/*-after.py` 完全一致，两项均为 `candidate_matches_after_snapshot=True`。
实际代码差异：

- 任务 1：`records/patches/20261102T145930828226Z-book_filter.diff`
- 任务 2：`records/patches/20261102T150240632353Z-book_filter.diff`

## 3. 上下文正确，且规格更新后使用新内容

任务 1 上下文（语料 v1，状态 seq 1）：

- `records/contexts/context-20261102T145823506159Z.json`
- `records/contexts/context-20261102T145823506159Z.md`
- 包含 `specs/book-filter.md`（artifact `art_7fbeb402278d438386ccec22f0104cf4`）、
  `tests/test_target_feature.py`（artifact `art_a2ef55f117da400cb088984f0f35d0e1`）与当前源码
  （artifact `art_4bb1ed0b296f4ceeb3a183b1b04cd7dd`）。

规格更新后重新导入（`init --refresh`，语料 v2）：

- 旧语料执行 PowerContext `forget`，新语料重新建索引；
- 新规格 artifact：`art_c9d3f06963914dd5929fd61d897f8ccc`；
- 新目标测试 artifact：`art_d6e2723053894026a974c3e1f5a47586`；
- 任务 2 上下文 `records/contexts/context-20261102T150220739369Z.md` 第 143 行起包含 v2 新增的
  `R5 最低价格（包含边界，v2 新增）`，同文第 161 行起包含新测试文件内容。

`solve` 在开始前会校验 `context.corpus_version == 当前 manifest.version`，版本不一致直接报错，
从机制上避免继续使用旧规格。

## 4. 数据库实际参与

- 语料、全文/向量索引、manifest、任务状态全部存放在 seekdb 的 PowerContext Scope 中。
- 任务上下文中的规格/测试片段来自 `Atomic Memory` 的 `hybrid` 检索，提示词里保留
  `artifact id`、`score`、`matched_by`；
- 当前源码按 manifest 中的 artifact id 精确读取（`get`），不是直接读磁盘文件；
- `records/db-queries.jsonl` 共 33 条：`search` 9 条、`list` 22 条、`get` 2 条。
- `records/corpus-imports.jsonl` 记录 v1/v2 两次导入及 manifest artifact id。

## 5. 状态能够复用

| seq | 场景 | 状态 | artifact |
| --- | --- | --- | --- |
| 1 | 任务 1 开始前，空验证结果 | `planned` | `art_ad863955e0484eac91780e4cac4308ca` |
| 2 | 任务 1 完成，含目标/回归结果与 diff | `done` | `art_f37ba5ee22944b65b587ca10e76694f0` |
| 3 | 规格 v2 导入后登记任务 2 | `planned` | `art_e212f54b806c4940aeadbac4bd1d8726` |
| 4 | 任务 2 完成，含目标/回归结果与 diff | `done` | `art_1bca99426cef440abfe400bfc7b32b8a` |

- 写入 seq 1 后，用**新进程**执行 `status`，读到 `latest seq=1 status=planned corpus=v1`；
- 任务 2 的上下文文件第 23–25 行读取到 `seq 2 [done]` 与 `seq 1 [planned]`，说明状态在
  重启、语料刷新之后仍被复用；
- 两个任务完成后，新进程 `status` 读到 `latest seq=4 status=done`。

## 6. 代码修改有效

| 阶段 | 目标测试 | 回归测试 | 日志 |
| --- | --- | --- | --- |
| 任务 1 修改前 | `5 failed` | — | `records/test-runs/20261102T145930828226Z-before-target.log` |
| 任务 1 修改后 | `5 passed` | `5 passed` | `records/test-runs/20261102T145930828226Z-attempt2-target.log` / `...-regression.log` |
| 任务 2 修改前 | `3 failed` | — | `records/test-runs/20261102T150240632353Z-before-target.log` |
| 任务 2 修改后 | `3 passed` | `5 passed` | `records/test-runs/20261102T150240632353Z-attempt1-target.log` / `...-regression.log` |
| 最终全量 | — | `13 passed` | `records/test-runs/final-full-suite.log` |

同一组目标测试在修改前失败、修改后全部通过；回归测试在修改前后均通过。
两次 `patches/*.diff` 只修改 `demo_project/src/book_filter.py`，未触碰测试或规格；
代码与测试的前后快照在 `records/snapshots/`。

## 复现提示

提交中的 `demo_project` 是两次任务完成后的最终状态。若要从头复现「修改前失败」：

1. 把 `records/snapshots/20261102T145930828226Z-before.py` 覆盖到
   `demo_project/src/book_filter.py`；
2. 把 `records/snapshots/book-filter.spec-v1.md` 覆盖到 `demo_project/specs/book-filter.md`，
   并暂时移除 `tests/test_target_price_range.py`；
3. 执行 `python -m sdd_context init --reset`，再按 README 的顺序执行。

第二次任务则使用 `records/snapshots/book-filter.spec-v2.md` 与 v2 测试文件，执行
`init --refresh` 后重跑。
