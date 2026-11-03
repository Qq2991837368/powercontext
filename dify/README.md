# 记忆罐头铺 · Dify 智能应用

用 Dify 工作流把一小段日常记忆「封存」成罐头，再用 PowerContext（seekdb）的混合检索，从货架上打捞几条相关的旧罐头作为「回声」。

赛题：**Just for Fun —— 用 Dify 做一个好玩的智能应用**。应用必须使用 Dify，并实际使用 seekdb / PowerContext 中至少一种，本项目两者都用了。

- Dify 负责：表单输入、编排、LLM 生成卡片、结果输出。
- seekdb + PowerContext 负责：记忆的持久化、向量与关键词混合检索。
- 配套 companion 服务负责：把 Dify 的 HTTP 请求翻译成 PowerContext 调用，并做一点「罐头铺」的玩法包装。

## 一、玩法

1. 在 Dify 里填写「今天想封存的记忆」，可选心情与口味，也可以全交给掌柜。
2. 应用的 HTTP 节点调用 companion，把这罐记忆写进 seekdb 中的一个 PowerContext scope：
   - 自动编罐号（`#0007`）、自动猜心情、随机抽一个口味（桂花 / 海风 / 旧歌 / 雨夜 / 橘子汽水 / 晒过太阳的被子）。
   - 用**混合检索**（关键词 + 向量）在旧罐头里找最多 3 条最像的回声。
3. LLM 节点把这段记忆和回声写成一张「罐头标签卡」：
   - 罐头标签、罐头正文、旧罐头回声、今日小任务。
4. 货架会随着你写下的记忆变多，回声也会越来越像你自己。

首次启动会放入 6 罐店铺示例记忆（`seeded=true`），保证第一次使用就有回声可听；示例记忆会在返回结果里明确标注，不会被当成用户本人的经历。

## 二、架构与数据流

```text
Dify Workflow（应用「记忆罐头铺」）
  start(memory, mood, flavor)
        │
        ▼
  http-request  POST http://host.docker.internal:8765/capsule
        │        （x-www-form-urlencoded，避免中文被转义破坏）
        ▼
  companion (FastAPI + PowerContext 1.4.1)
        │
        ├── open_builtin_runtime(BuiltinConfig(database=SeekDBConfig(path=/data/seekdb)),
        │                          embedding_model=LocalHashEmbedder())
        ├── atomic_memory.create(...)   写入罐头 JSON
        └── atomic_memory.search(text, mode="hybrid", kind="memory-capsule",
                                 admission=AdmissionFloor(...))  召回旧罐头
        │
        ▼
  llm（OpenAI-API-compatible，gpt-4o-mini 或任意兼容模型）
        ▼
  end → card / capsule_data / status_code
```

## 三、目录

```text
dify/
├── memory-cannery.dify.yml        # Dify DSL（0.7.0），直接导入即可
├── companion/                     # 配套服务：PowerContext + seekdb + FastAPI
│   ├── app.py                     # /health /capsule /capsules
│   ├── store.py                   # 罐头的写入、统计、混合检索回声
│   ├── embedder.py                # 本地确定性 384 维 embedding（无需外部 API）
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   └── requirements.txt
├── tools/
│   └── mock_llm.py                # 可选：离线联调用的 OpenAI 兼容桩，不属于应用本体
└── docs/
    └── VERIFICATION.md            # 真实运行记录（命令与输出）
```

## 四、启动 companion

seekdb 需要 Linux 原生文件系统，**不要**把数据目录放在 Windows 挂载盘上。推荐直接用 Docker。

```bash
cd dify/companion
docker compose up -d --build
docker compose ps
curl http://localhost:8765/health
```

预期（首次启动，干净数据卷）：

```json
{"ok":true,"service":"memory-cannery","scope_id":"scp_...","storage":"seekdb",
 "memory":"PowerContext atomic memory","pantry_total":6,"user_total":0,
 "today_total":0,"seeded_total":6}
```

数据保存在 compose 卷 `memory-cannery_cannery-data`（容器内 `/data/seekdb`），`docker compose down` 不会删除，`down -v` 才会。

也可以用原生 Python 在 WSL / Linux 上跑：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
CANNERY_STATE_DIR=$HOME/cannery-state uvicorn app:app --host 0.0.0.0 --port 8765
```

## 五、Dify 侧配置

环境：Dify 1.18.0 自托管（`docker compose`），工作流应用。

1. **导入 DSL**：工作室 / 应用列表 → 导入 DSL 文件 → 选择 `memory-cannery.dify.yml`。
   DSL 版本 `0.7.0`，Dify 1.18.0 可直接导入（已在 API 上做过导入回环验证，`warnings: []`）。
2. **安装插件**：DSL 声明了 `langgenius/openai_api_compatible:0.0.73`。导入时按提示安装；也可在「插件」页搜索 OpenAI-API-compatible 安装。
3. **配置模型**：设置 → 模型供应商 → OpenAI-API-compatible → 添加模型
   - 模型名必须与 LLM 节点里的 `gpt-4o-mini` 一致，或把节点模型改成你已配置的名字
   - Base URL / API Key 填你自己的 OpenAI 兼容服务
   - 该供应商要求 `context_size`、`max_tokens_to_sample` 等字段以字符串形式提交（在 UI 上按表单填写即可）
4. **放行内网地址**：Dify 的 SSRF proxy 默认禁止访问内网。若 companion 跑在宿主机（`host.docker.internal`），在 Dify 的 `docker/envs/infrastructure/ssrf-proxy.env` 中追加：

   ```bash
   SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.65.0/24,172.16.0.0/12
   SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal
   ```

   然后 `docker compose up -d ssrf_proxy api worker`。
5. **改 URL（可选）**：HTTP 节点默认 `http://host.docker.internal:8765/capsule`。
   - Docker Desktop（Windows/macOS）：保持默认即可。
   - Linux：改成 `http://172.17.0.1:8765/capsule` 或 companion 容器名。
   - 也可把 companion 与 Dify 放进同一 Docker 网络，用服务名访问。
6. **运行**：在工作流页面点「运行」，填一条记忆即可。

## 六、样例输入 / 输出

输入（真实运行，干净数据卷）：

```json
{"memory": "外婆在秋天总把桂花晒干，装进玻璃瓶里，说这样整个冬天都能闻到秋天。",
 "mood": "自动", "flavor": "随机"}
```

companion 返回（节选）：

```json
{"ok":true,
 "capsule":{"can_no":7,"user_can_no":1,"mood":"温柔","flavor":"旧歌","seeded":false},
 "echoes":[{"can_no":3,"text":"外婆家院子里有一棵桂花树，秋天一开门，整条巷子都是甜的。",
            "score":0.0323,"matched_by":["text","vector"],"seeded":true], "...": "..."},
 "stats":{"pantry_total":7,"user_total":1,"today_total":1,"seeded_total":6},
 "first_user_can":true}
```

Dify 工作流最终输出（`card` 字段，mock 模型生成，真实 LLM 结构相同）：

```text
【记忆罐头 #0009】
口味：晒过太阳的被子 · 心情：好笑
封存时间：2026-11-03 13:46

「收拾抽屉时翻到一张旧照片，背面写着十年前的海边，照片里的人都笑得有点傻。」

—— 从旧罐头里听见的回声 ——
· #0004（2026-10-27 23:05） 第一次一个人坐夜车，窗外只有路灯，耳机里循环着一首老歌。
· #0006（2026-11-01 22:50） 末班地铁到站时下起了小雨，站台上只剩自动贩卖机的灯亮着。
· #0005（2026-10-30 16:30） 海边捡到一枚被磨圆的玻璃片，对着太阳看，像一颗绿色的糖。

货架现在共有 9 罐，其中 3 罐来自客人。

今日小任务：把这罐记忆的某一个细节，讲给一个今天还没说过话的人听。
```

完整运行记录见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 七、API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态与货架统计 |
| POST | `/capsule` | 封存一罐记忆，返回新罐头、最多 3 条回声、统计 |
| GET | `/capsules?limit=6` | 最近的罐头（上限 20），便于人工核对 |

`POST /capsule` 同时接受 `application/json` 与 `application/x-www-form-urlencoded`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `memory` | string，1–1000 字 | 必填，记忆正文 |
| `mood` | string | `自动` 或 温柔/好笑/遗憾/平静/奇妙，非法值回退为自动猜测 |
| `flavor` | string | `随机` 或 六种口味之一，非法值回退为随机 |

返回字段：`capsule`（`can_no` / `user_can_no` / `text` / `mood` / `flavor` / `created_at` / `created_at_label` / `seeded` / `capsule_id`）、`echoes`（含 `score`、`matched_by`）、`stats`、`first_user_can`、`mood_auto`、`flavor_random`、`note`。

## 八、seekdb / PowerContext 实际作用

- `open_builtin_runtime(BuiltinConfig(database=SeekDBConfig(path=...)))`：以 seekdb 作为唯一持久化后端，数据落在容器 `/data/seekdb`，重启后罐号与货架完整保留。
- `runtime.scopes.create(ScopeDraft(...))` + `atomic_memory.for_scope(scope_id)`：所有罐头写进同一个 scope，用 `idempotency_key` 保证重复启动不会重建货架。
- `AtomicMemoryContent(kind="memory-capsule", text=json)`：一条罐头 = 一条原子记忆，JSON 里带罐号、心情、口味、时间，`seeded` 区分店铺示例。
- `atomic_memory.search(text, mode="hybrid", kind="memory-capsule", admission=...)`：回声来自真实的**混合检索**，返回 `matched_by: ["text","vector"]` 可核对关键词与向量两路都命中。
- `AdmissionFloor` 放宽为 `lexical_coverage=0.0 / lexical_min_matched_terms=1 / min_semantic_similarity=0.0`：中文短句在默认准入条件下容易只召回 1 条，放宽后才能凑齐 3 条回声；这个取舍在 `store.py` 里有注释。
- 检索只用记忆正文，不把随机口味拼进 query，避免「随机抽到橘子汽水」把无关旧罐头带出来。

`embedder.py` 提供本地确定性 embedding（384 维、blake2b 哈希词袋、ASCII 1.2 / CJK 双字 1.0 / CJK 单字 0.45、L2 归一化），不依赖外部 embedding API，离线也能完整跑通混合检索。

## 九、离线联调（可选）

没有真实模型 Key 时，可用 `tools/mock_llm.py` 起一个 OpenAI 兼容桩，仅用于验证 Dify 接线：

```bash
python tools/mock_llm.py          # 0.0.0.0:8010，模型名 gpt-4o-mini
```

在 Dify 里把 OpenAI-API-compatible 的 Base URL 指向 `http://host.docker.internal:8010/v1`，API Key 随便填。它只把 `<capsule_data>` 里的 JSON 拼成固定格式的卡片，**不是应用本体**，也不会生成新的文学内容；正式体验请换回真实模型。

## 十、已知限制

- seekdb 只支持 Linux 原生文件系统，Windows 上请用 Docker / WSL；不要把数据目录放在 `/mnt/d` 之类挂载盘。
- 单条记忆正文上限 8192 UTF-8 字节（PowerContext 限制），应用层已限制为 1000 字。
- 心情是关键词规则粗猜（`store.py: guess_mood`），只用于展示与检索，不是心理判断。
- `host.docker.internal` 依赖 Docker Desktop；Linux 宿主机需按第五节第 5 条改 URL。
- 本仓库验证使用的模型是 `tools/mock_llm.py`，因此示例文案是模板生成；换成真实模型后由 LLM 按同一提示词自由撰写。
