"""记忆罐头铺的 PowerContext/seekdb 存储层。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from powercontext.builtin.artifacts.search import AdmissionFloor

CAPSULE_KIND = "memory-capsule"
SCOPE_TITLE = "记忆罐头铺"
SCOPE_SUMMARY = (
    "把日常的一小块记忆封存成罐头，并在之后的访问里通过混合检索打捞相关的旧罐头回声。"
)
CST = timezone(timedelta(hours=8))
_PAGE_SIZE = 100
_FLAVORS = ("海风", "桂花", "旧歌", "雨夜", "橘子汽水", "晒过太阳的被子")
_MOODS = ("温柔", "好笑", "遗憾", "平静", "奇妙")
_MOOD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("好笑", ("笑", "尴尬", "糗", "哈哈", "离谱")),
    ("遗憾", ("最后", "错过", "没去", "忘了", "再见", "来不及")),
    ("温柔", ("外婆", "奶奶", "妈妈", "家", "猫", "阳光", "暖")),
    ("平静", ("夜", "一个人", "安静", "地铁", "窗", "雨")),
    ("奇妙", ("梦", "星星", "海", "玻璃", "云", "光")),
)

SEED_CAPSULES: tuple[dict[str, str], ...] = (
    {
        "text": "小时候把橘子汽水藏在床底，结果忘了，暑假结束才找到，瓶盖已经锈了。",
        "mood": "好笑",
        "flavor": "橘子汽水",
        "created_at": "2026-10-18T21:10:00+08:00",
    },
    {
        "text": "大学毕业那天下了很大的雨，我们躲在便利店门口分吃一包关东煮。",
        "mood": "温柔",
        "flavor": "雨夜",
        "created_at": "2026-10-21T19:40:00+08:00",
    },
    {
        "text": "外婆家院子里有一棵桂花树，秋天一开门，整条巷子都是甜的。",
        "mood": "温柔",
        "flavor": "桂花",
        "created_at": "2026-10-24T08:20:00+08:00",
    },
    {
        "text": "第一次一个人坐夜车，窗外只有路灯，耳机里循环着一首老歌。",
        "mood": "平静",
        "flavor": "旧歌",
        "created_at": "2026-10-27T23:05:00+08:00",
    },
    {
        "text": "海边捡到一枚被磨圆的玻璃片，对着太阳看，像一颗绿色的糖。",
        "mood": "奇妙",
        "flavor": "海风",
        "created_at": "2026-10-30T16:30:00+08:00",
    },
    {
        "text": "末班地铁到站时下起了小雨，站台上只剩自动贩卖机的灯亮着。",
        "mood": "平静",
        "flavor": "雨夜",
        "created_at": "2026-11-01T22:50:00+08:00",
    },
)


def now_cst() -> datetime:
    """返回北京时间。"""
    return datetime.now(CST)


def guess_mood(text: str) -> str:
    """按关键词给记忆一个粗略心情；只用于检索与展示，不宣称是心理判断。"""
    for mood, keywords in _MOOD_RULES:
        if any(keyword in text for keyword in keywords):
            return mood
    return "奇妙"


def parse_capsule(text: str) -> dict[str, Any] | None:
    """解析存储的记忆 JSON；不是合法 JSON 时返回 None。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _iso(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("T", " ")[:16]


class Cannery:
    """封装一个 scope 下的罐头写入与回声检索。"""

    def __init__(self, memory: Any, scope_id: str) -> None:
        self._memory = memory
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        return self._scope_id

    async def _create(self, text: str) -> str:
        from powercontext.builtin.runtime.models import AtomicMemoryContent

        result = await self._memory.create((AtomicMemoryContent(kind=CAPSULE_KIND, text=text),))
        return result.records[0].ref.artifact_id

    async def _list_all(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: Any = None
        while True:
            page = await self._memory.list(kind=CAPSULE_KIND, limit=_PAGE_SIZE, cursor=cursor)
            for item in page.items:
                payload = parse_capsule(item.artifact.content.text)
                if not payload:
                    continue
                payload["capsule_id"] = item.ref.artifact_id
                items.append(payload)
            next_cursor = getattr(page, "next_cursor", None)
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        items.sort(key=lambda item: int(item.get("can_no", 0)))
        return items

    async def count(self) -> int:
        return len(await self._list_all())

    async def stats(self) -> dict[str, Any]:
        capsules = await self._list_all()
        today = now_cst().date().isoformat()
        user_capsules = [item for item in capsules if not item.get("seeded")]
        return {
            "pantry_total": len(capsules),
            "user_total": len(user_capsules),
            "today_total": sum(
                1
                for item in capsules
                if str(item.get("created_at", ""))[:10] == today
            ),
            "seeded_total": len(capsules) - len(user_capsules),
        }

    async def add(
        self,
        text: str,
        mood: str,
        flavor: str,
        *,
        created_at: str | None = None,
        seeded: bool = False,
    ) -> dict[str, Any]:
        capsules = await self._list_all()
        can_no = len(capsules) + 1
        user_can_no = sum(1 for item in capsules if not item.get("seeded")) + 1
        payload = {
            "v": 1,
            "can_no": can_no,
            "user_can_no": user_can_no,
            "text": text,
            "mood": mood,
            "flavor": flavor,
            "created_at": created_at or now_cst().isoformat(),
            "seeded": seeded,
        }
        payload["created_at_label"] = _iso(payload["created_at"])
        artifact_id = await self._create(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        payload["capsule_id"] = artifact_id
        return payload

    async def ensure_seeds(self) -> None:
        """首次启动时放入 6 罐店铺示例记忆，让第一次访问也有回声。"""
        if await self.count() > 0:
            return
        for seed in SEED_CAPSULES:
            await self.add(seed["text"], seed["mood"], seed["flavor"], created_at=seed["created_at"], seeded=True)

    async def echoes(
        self,
        text: str,
        *,
        exclude_id: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """用混合检索打捞相关的旧罐头；只用记忆正文，避免随机口味干扰召回。"""
        page = await self._memory.search(
            text,
            mode="hybrid",
            kind=CAPSULE_KIND,
            limit=max(limit + 4, 8),
            admission=AdmissionFloor(
                lexical_coverage=0.0,
                lexical_min_matched_terms=1,
                min_semantic_similarity=0.0,
            ),
        )
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in page.hits:
            artifact_id = hit.hit.artifact_ref.artifact_id
            if artifact_id == exclude_id or artifact_id in seen:
                continue
            payload = parse_capsule(hit.text)
            if not payload:
                continue
            payload["capsule_id"] = artifact_id
            payload["score"] = round(float(hit.hit.score), 4)
            raw = getattr(hit, "matched_by", None)
            if raw is None:
                matched_by: list[str] = []
            elif isinstance(raw, str):
                matched_by = [raw]
            else:
                matched_by = [str(value) for value in raw]
            payload["matched_by"] = matched_by
            payload["created_at_label"] = _iso(payload.get("created_at"))
            results.append(payload)
            seen.add(artifact_id)
            if len(results) >= limit:
                break
        return results

    async def recent(self, limit: int = 6) -> list[dict[str, Any]]:
        capsules = await self._list_all()
        capsules.reverse()
        results: list[dict[str, Any]] = []
        for payload in capsules[:limit]:
            payload["created_at_label"] = _iso(payload.get("created_at"))
            results.append(payload)
        return results
