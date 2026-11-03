"""记忆罐头铺的 HTTP 配套服务：Dify 通过 HTTP 请求节点调用。"""

from __future__ import annotations

import os
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from embedder import LocalHashEmbedder
from store import Cannery, SCOPE_SUMMARY, SCOPE_TITLE, _FLAVORS, _MOODS, guess_mood
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.scope import ScopeDraft


class CapsuleIn(BaseModel):
    """Dify 提交的一罐新记忆。"""

    memory: str = Field(..., min_length=1, max_length=1000)
    mood: str = Field(default="自动")
    flavor: str = Field(default="随机")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时打开 seekdb/PowerContext，并放入店铺示例罐头。"""
    state_dir = Path(os.environ.get("CANNERY_STATE_DIR", "./state"))
    db_dir = state_dir / "seekdb"
    db_dir.mkdir(parents=True, exist_ok=True)
    config = BuiltinConfig(database=SeekDBConfig(path=db_dir))
    async with open_builtin_runtime(config, embedding_model=LocalHashEmbedder()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(
                title=SCOPE_TITLE,
                summary=SCOPE_SUMMARY,
                idempotency_key=os.environ.get("CANNERY_SCOPE_KEY", "memory-cannery-v1"),
            )
        )
        cannery = Cannery(runtime.atomic_memory.for_scope(scope.scope_id), scope.scope_id)
        await cannery.ensure_seeds()
        app.state.cannery = cannery
        yield


app = FastAPI(title="记忆罐头铺", version="1.0.0", lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health() -> dict[str, Any]:
    """返回服务与罐头的健康状态。"""
    cannery: Cannery = app.state.cannery
    stats = await cannery.stats()
    return {
        "ok": True,
        "service": "memory-cannery",
        "scope_id": cannery.scope_id,
        "storage": "seekdb",
        "memory": "PowerContext atomic memory",
        **stats,
    }


async def _read_capsule_input(request: Request) -> CapsuleIn:
    """同时接受 JSON 与表单，Dify 的 x-www-form-urlencoded 不会破坏用户文本。"""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
    else:
        form = await request.form()
        data = {key: str(value) for key, value in form.items()}
    try:
        return CapsuleIn.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@app.post("/capsule")
async def capsule(request: Request) -> dict[str, Any]:
    """封存一罐记忆，并打捞最多 3 条相关的旧罐头。"""
    body = await _read_capsule_input(request)
    cannery: Cannery = app.state.cannery
    memory = body.memory.strip()
    mood_auto = body.mood == "自动"
    mood = guess_mood(memory) if mood_auto else (body.mood if body.mood in _MOODS else guess_mood(memory))
    flavor_random = body.flavor == "随机"
    flavor = random.choice(_FLAVORS) if flavor_random else (body.flavor if body.flavor in _FLAVORS else "随机")
    if flavor == "随机":
        flavor = random.choice(_FLAVORS)

    before = await cannery.stats()
    record = await cannery.add(memory, mood, flavor)
    echoes = await cannery.echoes(memory, exclude_id=record["capsule_id"], limit=3)
    after = await cannery.stats()

    return {
        "ok": True,
        "capsule": record,
        "echoes": echoes,
        "stats": after,
        "first_user_can": before["user_total"] == 0,
        "mood_auto": mood_auto,
        "flavor_random": flavor_random,
        "note": "seeded=true 的是店铺示例记忆，不是用户本人的经历。",
    }


@app.get("/capsules")
async def capsules(limit: int = 6) -> dict[str, Any]:
    """返回最近的罐头，用于人工验证。"""
    cannery: Cannery = app.state.cannery
    items = await cannery.recent(limit=max(1, min(limit, 20)))
    stats = await cannery.stats()
    return {"ok": True, "items": items, "stats": stats}
