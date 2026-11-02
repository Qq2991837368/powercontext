# 技术使用记录

本目录保存了提交版本对应的真实运行证据。所有时间均为 UTC。

## 1. MiniMax 开发记录

| 任务 | 轮次 | 会话文件 | response id | finish | 用量（total / prompt / completion / reasoning） | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 任务 1（规格 v1：R2/R3/R4） | 1 | `minimax-sessions/20261102T145930828226Z-attempt1.json` | 0737d85327ae9e5a72c255db88c69168 | length | 5741 / 1645 / 4096 / 3880 | 输出截断，拒绝 |
| 任务 1 | 2 | `minimax-sessions/20261102T145930828226Z-attempt2.json` | 0737d86b2e00f7789bfeaf6e8e429512 | stop | 4685 / 1674 / 3011 / 2361 | 通过并写回状态 |
| 任务 2（规格 v2：R5） | 1 | `minimax-sessions/20261102T150240632353Z-attempt1.json` | 0737d911fa8278d90e148672c83752d8 | stop | 3234 / 2162 / 1072 / 330 | 通过并写回状态 |

会话文件包含完整的 `messages`（含从数据库生成的上下文）与模型的 `content` / `reasoning_content` /
`usage`，不包含 Authorization 头或 API Key。

`session-integrity.log` 校验结果：

- 任务 1：`candidate_matches_after_snapshot=True`
- 任务 2：`candidate_matches_after_snapshot=True`

即模型响应中的完整文件与最终落盘快照逐字节一致。

## 2. 数据库查询记录

`db-queries.jsonl` 共 33 条：

- `search` 9 条（任务 1/2 的 spec、code、test 混合检索）
- `list` 22 条（读取最新 manifest、状态历史、刷新语料前的旧记录）
- `get` 2 条（按 manifest artifact id 精确读取当前源码）

每条记录包含 `scope_id`、`operation`、`kind`、query、命中数量、artifact id、score、
`matched_by` 与时间。示例含义：

- `operation="list", kind="task-state"`：读取状态历史并选择最大 `seq`；
- `operation="search", kind="spec"`：混合检索规格，返回 artifact id 与 `matched_by`；
- `operation="get", kind="code"`：精确读取待修改源码。

## 3. 语料导入记录

`corpus-imports.jsonl`：

| 版本 | 方式 | 文件数 | 说明 |
| --- | --- | --- | --- |
| 1 | `init --reset` | 4 | 规格 v1 + 初始源码 + 回归测试 + 目标测试 |
| 2 | `init --refresh` | 5 | 规格 v2 + 任务 1 后源码 + 回归 + 两个目标测试 |

每次导入都记录文件路径、sha256、artifact id 与 manifest artifact id。

## 4. PowerContext 状态写入与复用记录

`state-events.jsonl`：

| seq | status | artifact id | 内容 |
| --- | --- | --- | --- |
| 1 | planned | `art_ad863955e0484eac91780e4cac4308ca` | 任务 1 开始，verification 为空 |
| 2 | done | `art_f37ba5ee22944b65b587ca10e76694f0` | 任务 1 验证结果、diff、日志、会话 |
| 3 | planned | `art_e212f54b806c4940aeadbac4bd1d8726` | 规格 v2 导入后登记任务 2 |
| 4 | done | `art_1bca99426cef440abfe400bfc7b32b8a` | 任务 2 验证结果、diff、日志、会话 |

复用证据：

- seq 1 写入后，新进程 `status` 输出 `latest seq=1 status=planned corpus=v1`；
- 任务 2 上下文 `contexts/context-20261102T150220739369Z.md` 的 `Earlier states` 段读取到
  `seq 2 [done]` 与 `seq 1 [planned]`；
- 两次任务完成后，新进程 `status` 输出 `latest seq=4 status=done`。

## 5. 测试记录

- `test-runs/20261102T145930828226Z-before-target.log`：任务 1 修改前 `5 failed`
- `test-runs/20261102T145930828226Z-attempt2-target.log`：任务 1 目标 `5 passed`
- `test-runs/20261102T145930828226Z-attempt2-regression.log`：任务 1 回归 `5 passed`
- `test-runs/20261102T150240632353Z-before-target.log`：任务 2 修改前 `3 failed`
- `test-runs/20261102T150240632353Z-attempt1-target.log`：任务 2 目标 `3 passed`
- `test-runs/20261102T150240632353Z-attempt1-regression.log`：任务 2 回归 `5 passed`
- `test-runs/final-full-suite.log`：最终全量 `13 passed`

## 6. 代码与规格快照

- `snapshots/20261102T145930828226Z-before.py` / `-after.py`：任务 1 代码前后
- `snapshots/20261102T150240632353Z-before.py` / `-after.py`：任务 2 代码前后
- `snapshots/book-filter.spec-v1.md` / `book-filter.spec-v2.md`：规格前后
- `patches/*.diff`：两次实际代码 diff
- `snapshots/SHA256SUMS`：任务 1 `-after.py` 与任务 2 `-before.py` 哈希相同
  （`d1bc0a16…`），任务 2 `-after.py` 与 `demo_project/src/book_filter.py` 哈希相同
  （`de05c922…`），证明两次会话与最终代码连续对应。
