# 验证记录 · 记忆罐头铺

验证时间：2026-11-03（Asia/Shanghai）
环境：Windows 11 + Docker Desktop；Dify 1.18.0 自托管（`langgenius/dify-api:1.18.0`）；companion 容器 `python:3.12-slim` + `powercontext[seekdb]==1.4.1`。

> 说明：本机没有可用的真实模型 Key，LLM 节点在验证时指向本地 `tools/mock_llm.py`（OpenAI 兼容桩）。因此下文卡片文案是模板生成的；工作流结构、HTTP 节点、seekdb/PowerContext 检索与持久化均为真实运行结果。换成真实模型后提示词与节点不变。

## 1. companion 启动

```bash
$ cd dify/companion && docker compose up -d --build
$ docker compose ps
NAME              STATUS
memory-cannery    Up (healthy)
```

干净数据卷首次启动（`GET /health`）：

```json
{"ok":true,"service":"memory-cannery","scope_id":"scp_6xjt4fd6d34g797az1j6b73dx2",
 "storage":"seekdb","memory":"PowerContext atomic memory",
 "pantry_total":6,"user_total":0,"today_total":0,"seeded_total":6}
```

结论：seekdb 正常打开，6 罐店铺示例已写入，用户罐头为 0。

## 2. 表单提交（Dify HTTP 节点的真实编码方式）

```bash
$ curl -s -X POST http://127.0.0.1:8765/capsule \
    --data-urlencode 'memory=外婆在秋天总把桂花晒干，装进玻璃瓶里，说这样整个冬天都能闻到秋天。' \
    --data-urlencode 'mood=自动' --data-urlencode 'flavor=随机'
```

```json
{"ok":true,
 "capsule":{"v":1,"can_no":7,"user_can_no":1,
   "text":"外婆在秋天总把桂花晒干，装进玻璃瓶里，说这样整个冬天都能闻到秋天。",
   "mood":"温柔","flavor":"旧歌","created_at":"2026-11-03T13:45:58+08:00",
   "created_at_label":"2026-11-03 13:45","seeded":false,
   "capsule_id":"art_197c62ac9e354d7f9c87f7ebd6f63778"},
 "echoes":[
   {"can_no":3,"text":"外婆家院子里有一棵桂花树，秋天一开门，整条巷子都是甜的。",
    "score":0.0323,"matched_by":["text","vector"],"seeded":true},
   {"can_no":1,"text":"小时候把橘子汽水藏在床底，结果忘了，暑假结束才找到，瓶盖已经锈了。",
    "score":0.0159,"matched_by":["vector"],"seeded":true},
   {"can_no":4,"text":"第一次一个人坐夜车，窗外只有路灯，耳机里循环着一首老歌。",
    "score":0.0159,"matched_by":["text"],"seeded":true}],
 "stats":{"pantry_total":7,"user_total":1,"today_total":1,"seeded_total":6},
 "first_user_can":true,"mood_auto":true,"flavor_random":true,
 "note":"seeded=true 的是店铺示例记忆，不是用户本人的经历。"}
```

核对点：
- `mood_auto=true`，关键词规则把「外婆 / 秋天」判成「温柔」。
- `flavor_random=true`，随机抽到「旧歌」；回声排序**没有**被口味带偏，第一名仍是桂花记忆。
- 第一名 `matched_by` 同时含 `text` 与 `vector`，说明混合检索两路都命中。

## 3. JSON 提交 + 中文完整性

```bash
$ curl -s -X POST http://127.0.0.1:8765/capsule -H 'Content-Type: application/json' \
    --data-binary '{"memory":"加班到十一点，出地铁时在下雨，没带伞，就站在便利店的屋檐下看雨把霓虹灯泡软。","mood":"平静","flavor":"雨夜"}'
```

```json
{"ok":true,
 "capsule":{"can_no":8,"user_can_no":2,"mood":"平静","flavor":"雨夜","seeded":false,
   "created_at_label":"2026-11-03 13:46"},
 "echoes":[
   {"can_no":2,"text":"大学毕业那天下了很大的雨，我们躲在便利店门口分吃一包关东煮。",
    "score":0.032,"matched_by":["text","vector"]},
   {"can_no":6,"text":"末班地铁到站时下起了小雨，站台上只剩自动贩卖机的灯亮着。",
    "score":0.032,"matched_by":["text","vector"]},
   {"can_no":5,"text":"海边捡到一枚被磨圆的玻璃片，对着太阳看，像一颗绿色的糖。",
    "score":0.0156,"matched_by":["vector"]}],
 "stats":{"pantry_total":8,"user_total":2,"today_total":2,"seeded_total":6},
 "first_user_can":false,"mood_auto":false,"flavor_random":false}
```

核对点：两条回声与「雨 / 便利店 / 地铁」直接相关；中文正文与时间标签完整（无 Latin-1 乱码）。

> 注意：用 PowerShell 5.1 的 `Invoke-RestMethod` 提交表单会把中文按 Latin-1 编码写坏，这是客户端问题；请用 `curl --data-urlencode`（如上）或 JSON 提交。Dify 的 HTTP 节点使用 `x-www-form-urlencoded` 且按 UTF-8 编码，实测正常。

## 4. 最近罐头

```bash
$ curl -s 'http://127.0.0.1:8765/capsules?limit=3'
```

```json
{"ok":true,"stats":{"pantry_total":8,"user_total":2,"today_total":2,"seeded_total":6},
 "items":[
   {"can_no":8,"text":"加班到十一点，出地铁时在下雨……","flavor":"雨夜","seeded":false},
   {"can_no":7,"text":"外婆在秋天总把桂花晒干……","flavor":"旧歌","seeded":false},
   {"can_no":6,"text":"末班地铁到站时下起了小雨，站台上只剩自动贩卖机的灯亮着。","seeded":true}]}
```

## 5. 持久化

```bash
$ docker restart memory-cannery
$ curl -s http://localhost:8765/health
```

```json
{"ok":true,"scope_id":"scp_6xjt4fd6d34g797az1j6b73dx2","pantry_total":8,
 "user_total":2,"today_total":2,"seeded_total":6}
```

核对点：`scope_id` 不变、8 罐全部保留，seekdb 数据在命名卷 `memory-cannery_cannery-data` 中。

## 6. Dify 工作流端到端

输入：

```json
{"inputs":{"memory":"收拾抽屉时翻到一张旧照片，背面写着十年前的海边，照片里的人都笑得有点傻。",
           "mood":"自动","flavor":"随机"},"files":[]}
```

节点结果（`POST /console/api/apps/<app_id>/workflows/draft/run`，SSE 解析）：

```text
start        succeeded  0.000s
capsule_http succeeded  0.954s   status_code=200
card_llm     succeeded  0.042s
end          succeeded  0.000s
workflow=succeeded elapsed=1.077s tokens=2133 exceptions=0
```

`card` 输出：

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

`capsule_data`（节选，来自 HTTP 节点真实响应）：

```json
{"ok":true,"capsule":{"can_no":9,"user_can_no":3,"mood":"好笑","flavor":"晒过太阳的被子",
  "seeded":false},"echoes":[{"can_no":4,"score":0.0315,"matched_by":["text","vector"]},
  {"can_no":6,"score":0.0161,"matched_by":["vector"]},
  {"can_no":5,"score":0.0161,"matched_by":["text"]}],
 "stats":{"pantry_total":9,"user_total":3,"today_total":3,"seeded_total":6}}
```

核对点：Dify → HTTP → seekdb 写入 → 混合检索 → LLM → End 全链路 1.1 秒完成，无异常；#0005「海边玻璃」被真实召回。

## 7. DSL 导入回环

用仓库里的 `memory-cannery.dify.yml` 直接调 Dify 导入接口：

```text
IMPORT status=completed app_id=a742f2d1-... app_mode=workflow dsl=0.7.0 warnings=
CLEANUP deleted a742f2d1-...
```

结论：DSL 可被 Dify 1.18.0 完整导入，无警告；导入产生的临时应用已删除。
