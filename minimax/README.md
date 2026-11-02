# 规有所依：MiniMax + seekdb + PowerContext 的 SDD 上下文管理

本目录是 OceanBase「规有所依——面向软件持续演进的 SDD 上下文管理系统」赛题的参赛实现，
基于 `powercontext` 仓库的 `2026-competition-ecosystem` 分支，全部资产位于 `minimax/`。

实现完成了一次真实的规格驱动开发（SDD）闭环：

1. 把规格、源码、测试导入 seekdb，并通过 PowerContext 建立工程记忆；
2. 按任务做混合检索，生成包含规格/代码/测试/历史状态的开发上下文；
3. 将上下文交给 MiniMax-M3.1-Flash 生成完整的新源码；
4. 运行目标测试与回归测试，把验证结果写回 PowerContext；
5. 更新规格、重新导入语料，再启动新进程读取历史状态并完成第二个开发任务；
6. 全程保留 MiniMax 会话、数据库查询、状态写入/复用与测试日志。

## 技术分工

| 组件 | 职责 |
| --- | --- |
| MiniMax-M3.1-Flash | 阅读开发上下文，生成/修改 `demo_project/src/book_filter.py` |
| seekdb（嵌入式，`pylibseekdb`） | 保存规格/源码/测试的原子记忆、全文与向量索引、语料清单 |
| PowerContext 1.4.1 | Scope、Atomic Memory、混合检索、语料刷新（forget）、任务状态持久化 |
| Python CLI | `init` / `context` / `record` / `status` / `solve`，负责编排与证据落盘 |

MiniMax 通过 OpenAI 兼容接口 `POST /v1/chat/completions` 调用；API Key 只从环境变量读取，
不会写入任何提交文件。

## 目录结构

```text
minimax/
├── sdd_context/          # 上下文管理 CLI
│   ├── embeddings.py     # 256 维确定性本地 embedding（离线可复现）
│   ├── store.py          # seekdb + PowerContext：语料/状态/检索
│   ├── context.py        # 生成上下文包与 MiniMax 提示词
│   ├── llm.py            # MiniMax 客户端
│   ├── pytest_runner.py  # 测试执行与日志
│   ├── solve.py          # 生成 → 应用 → 验证 → 写回状态
│   └── cli.py
├── demo_project/         # 内置迷你业务工程
│   ├── specs/book-filter.md
│   ├── src/book_filter.py
│   └── tests/            # 目标测试 + 回归测试
├── records/              # 已提交的真实运行证据
│   ├── contexts/         # 任务 1/2 的完整上下文（来自数据库）
│   ├── minimax-sessions/ # MiniMax 请求/响应（不含 Key）
│   ├── patches/          # 前后代码 diff
│   ├── snapshots/        # 代码/规格前后快照
│   ├── test-runs/        # pytest 原始日志
│   ├── db-queries.jsonl  # 数据库查询记录
│   ├── state-events.jsonl
│   ├── corpus-imports.jsonl
│   └── USAGE.md          # 技术使用记录索引
└── docs/
    ├── VERIFICATION.md   # 验收对照与真实结果
    └── TECH_NOTES.md     # 一页技术说明
```

## 环境要求

- Linux x86_64（验证环境：WSL2 Ubuntu 24.04，Python 3.12.3）
- `seekdb` 的数据库目录必须位于 **Linux 原生文件系统**。
  - 原生 Linux：默认 `.state/` 即可。
  - WSL2：请把状态目录放到 `/root` 等 ext4 路径，**不要**放在 `/mnt/d`（drvfs 上 `seekdb_open` 会失败）：

```bash
export SDD_STATE_DIR=/root/sdd-demo-state
```

- Python 依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行说明

### 1. 导入工程资料

```bash
python -m sdd_context init --reset
```

输出语料版本、文件列表、各自的 artifact id 与 manifest artifact id。
重新导入更新后的规格时使用：

```bash
python -m sdd_context init --refresh
```

`--refresh` 会先对旧语料执行 PowerContext `forget`，再写入新版本，保证检索只返回新内容。

### 2. 记录并跨进程读取任务状态

```bash
python -m sdd_context record \
  --task "实现规格 v1 的 R2/R3/R4" \
  --status planned \
  --summary "任务已登记"

# 新进程读取（模拟模块重启）
python -m sdd_context status --history 5
```

### 3. 构建开发上下文

```bash
python -m sdd_context context \
  --task "实现规格 v1 的 R2/R3/R4：max_price 闭区间上界、tags 全匹配、默认排除缺货" \
  --editable-file src/book_filter.py
```

会生成 `records/contexts/context-<id>.json` 与同名 `.md`。上下文包含：

- seekdb 混合检索得到的规格、测试片段（带 artifact id、score、matched_by）；
- 按 artifact id 精确读取的当前源码（`Working file`）；
- PowerContext 中最新的任务状态与历史状态。

### 4. MiniMax 修改 + 验证

```bash
export MINIMAX_API_KEY=...          # 仅在当前 shell 设置
CTX=$(ls -t records/contexts/context-*.json | head -1)

python -m sdd_context solve \
  --task "实现规格 v1 的 R2/R3/R4：max_price 闭区间上界、tags 全匹配、默认排除缺货" \
  --context "$CTX" \
  --editable-file src/book_filter.py \
  --target-tests tests/test_target_feature.py \
  --regression-tests tests/test_regression.py
```

`solve` 的行为：

1. 先跑目标测试并保存「修改前失败」日志；
2. 调用 MiniMax，保存完整请求/响应到 `records/minimax-sessions/`；
3. 校验响应中的完整文件、语法，再写入目标文件；
4. 跑目标测试与回归测试，保存日志、前后快照与 unified diff；
5. 成功则写回 `status=done` 的任务状态；失败则恢复原文件，并把失败输出反馈给 MiniMax 重试。

### 5. 规格演进（第二个任务）

修改 `demo_project/specs/book-filter.md`（本项目已从 v1 更新到 v2，新增 R5 `min_price`），新增
`tests/test_target_price_range.py`，然后：

```bash
python -m sdd_context init --refresh
python -m sdd_context record --task "实现规格 v2 新增的 R5" --status planned --summary "规格 v2 已重新导入"
python -m sdd_context context --task "实现规格 v2 新增的 R5：min_price 闭区间下界"
python -m sdd_context solve --task "实现规格 v2 新增的 R5：min_price 闭区间下界" \
  --context "$(ls -t records/contexts/context-*.json | head -1)" \
  --target-tests tests/test_target_price_range.py
python -m sdd_context status --history 5
```

`solve` 会校验上下文中的语料版本是否与数据库当前 manifest 一致，避免使用过期规格。

### 6. 全量回归

```bash
cd demo_project
python -m pytest tests -q --color=no
```

## 结果

- 任务 1：目标测试修改前 `5 failed` → 修改后 `5 passed`；回归 `5 passed`。
- 任务 2（规格 v2）：目标测试修改前 `3 failed` → 修改后 `3 passed`；回归 `5 passed`。
- 最终全量测试：`13 passed`。
- 详细证据、artifact id、会话与日志路径见 [docs/VERIFICATION.md](docs/VERIFICATION.md)，
  技术说明见 [docs/TECH_NOTES.md](docs/TECH_NOTES.md)，使用记录见 [records/USAGE.md](records/USAGE.md)。

## 安全说明

- 不在仓库中保存任何 MiniMax Key；`.env.example` 仅含占位符。
- `records/` 中的会话记录只包含请求消息与模型输出，不包含 Authorization 头。
- `.state/`、`.venv/`、`__pycache__/` 已加入 `.gitignore`。
