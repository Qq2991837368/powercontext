"""seekdb + PowerContext storage for engineering material and task state."""

from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, AsyncIterator

from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.runtime.models import AtomicMemoryContent
from powercontext.builtin.scope import ScopeDraft

from .config import SCOPE_SUMMARY, SCOPE_TITLE, Paths, scope_key
from .embeddings import LocalHashEmbedder

CORPUS_KINDS = ("spec", "code", "test")
MANIFEST_KIND = "corpus-manifest"
STATE_KIND = "task-state"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def source_of(text: str) -> str:
    first, _, _ = text.partition("\n")
    if first.startswith("[[SOURCE ") and first.endswith("]]"):
        return first[len("[[SOURCE ") : -2]
    return "unknown"


class Store:
    def __init__(self, runtime: Any, scope_id: str, paths: Paths) -> None:
        self.runtime = runtime
        self.scope_id = scope_id
        self.paths = paths
        self.memory = runtime.atomic_memory.for_scope(scope_id)

    # ------------------------------------------------------------------ corpus
    async def import_corpus(self, *, refresh: bool = False) -> dict[str, Any]:
        previous = await self.current_manifest()
        version = 1 if previous is None else int(previous["version"]) + 1
        files = self._collect_files()

        if refresh:
            forgotten = 0
            for kind in (*CORPUS_KINDS, MANIFEST_KIND):
                forgotten += await self._forget_kind(kind)
        else:
            forgotten = 0

        contents = tuple(
            AtomicMemoryContent(kind=item["kind"], text=f"[[SOURCE {item['path']}]]\n{item['content']}")
            for item in files
        )
        result = await self.memory.create(contents)
        artifact_ids = [record.ref.artifact_id for record in result.records]

        manifest = {
            "version": version,
            "imported_at": utc_now(),
            "scope_id": self.scope_id,
            "files": [
                {
                    "path": item["path"],
                    "kind": item["kind"],
                    "sha256": item["sha256"],
                    "artifact_id": artifact_id,
                }
                for item, artifact_id in zip(files, artifact_ids, strict=True)
            ],
        }
        manifest_content = AtomicMemoryContent(
            kind=MANIFEST_KIND,
            text=json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )
        manifest_result = await self.memory.create((manifest_content,))
        manifest["artifact_id"] = manifest_result.records[0].ref.artifact_id

        append_jsonl(
            self.paths.records / "corpus-imports.jsonl",
            {
                "at": utc_now(),
                "scope_id": self.scope_id,
                "version": version,
                "refresh": refresh,
                "forgotten": forgotten,
                "files": [item["path"] for item in files],
                "artifact_ids": artifact_ids,
                "manifest_artifact_id": manifest["artifact_id"],
            },
        )
        return manifest

    def _collect_files(self) -> list[dict[str, str]]:
        patterns = (("spec", "specs/*.md"), ("code", "src/*.py"), ("test", "tests/*.py"))
        files: list[dict[str, str]] = []
        for kind, pattern in patterns:
            for path in sorted(self.paths.demo.glob(pattern)):
                if path.name == "__init__.py":
                    continue
                raw = path.read_bytes()
                files.append(
                    {
                        "path": path.relative_to(self.paths.demo).as_posix(),
                        "kind": kind,
                        "sha256": sha256(raw).hexdigest(),
                        "content": raw.decode("utf-8"),
                    }
                )
        return files

    async def _forget_kind(self, kind: str) -> int:
        records = await self._list_kind(kind)
        for record in records:
            await self.memory.forget(
                record.ref.artifact_id,
                expected_revision=record.ref.revision,
                expected_state_version=record.state.state_version,
            )
        return len(records)

    async def _list_kind(self, kind: str) -> list[Any]:
        rows: list[Any] = []
        cursor: str | None = None
        while True:
            page = await self.memory.list(kind=kind, limit=100, cursor=cursor)
            rows.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return rows

    async def current_manifest(self) -> dict[str, Any] | None:
        rows = await self._list_kind(MANIFEST_KIND)
        manifests = []
        for record in rows:
            try:
                payload = json.loads(record.artifact.content.text)
            except json.JSONDecodeError:
                continue
            payload["artifact_id"] = record.ref.artifact_id
            manifests.append(payload)
        if not manifests:
            return None
        latest = max(manifests, key=lambda item: int(item.get("version", 0)))
        append_jsonl(
            self.paths.records / "db-queries.jsonl",
            {
                "at": utc_now(),
                "scope_id": self.scope_id,
                "operation": "list",
                "kind": MANIFEST_KIND,
                "returned": len(rows),
                "selected_version": latest["version"],
                "selected_artifact_id": latest["artifact_id"],
            },
        )
        return latest

    # ------------------------------------------------------------------- state
    async def state_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._list_kind(STATE_KIND)
        states: list[dict[str, Any]] = []
        for record in rows:
            try:
                payload = json.loads(record.artifact.content.text)
            except json.JSONDecodeError:
                continue
            payload["artifact_id"] = record.ref.artifact_id
            states.append(payload)
        states.sort(key=lambda item: int(item.get("seq", 0)), reverse=True)
        append_jsonl(
            self.paths.records / "db-queries.jsonl",
            {
                "at": utc_now(),
                "scope_id": self.scope_id,
                "operation": "list",
                "kind": STATE_KIND,
                "returned": len(rows),
                "selected_seq": states[0]["seq"] if states else None,
                "selected_artifact_id": states[0]["artifact_id"] if states else None,
            },
        )
        return states[:limit]

    async def latest_state(self) -> dict[str, Any] | None:
        history = await self.state_history(limit=1)
        return history[0] if history else None

    async def record_state(
        self,
        *,
        task: str,
        status: str,
        summary: str,
        verification: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        history = await self.state_history(limit=100)
        seq = max((int(item["seq"]) for item in history), default=0) + 1
        payload = {
            "seq": seq,
            "task": task,
            "status": status,
            "summary": summary,
            "verification": verification,
            "notes": notes,
            "created_at": utc_now(),
            "scope_id": self.scope_id,
        }
        result = await self.memory.create(
            (AtomicMemoryContent(kind=STATE_KIND, text=json.dumps(payload, ensure_ascii=False, sort_keys=True)),)
        )
        artifact_id = result.records[0].ref.artifact_id
        payload["artifact_id"] = artifact_id
        append_jsonl(self.paths.records / "state-events.jsonl", payload)
        return payload

    # ------------------------------------------------------------------ search
    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        record = await self.memory.get(artifact_id)
        text = record.artifact.content.text
        payload = {
            "kind": record.artifact.content.kind,
            "source": source_of(text),
            "artifact_id": record.ref.artifact_id,
            "revision": record.ref.revision,
            "text": text,
        }
        append_jsonl(
            self.paths.records / "db-queries.jsonl",
            {
                "at": utc_now(),
                "scope_id": self.scope_id,
                "operation": "get",
                "kind": payload["kind"],
                "source": payload["source"],
                "artifact_id": payload["artifact_id"],
                "revision": payload["revision"],
            },
        )
        return payload

    async def search(self, query: str, *, kind: str, limit: int) -> list[dict[str, Any]]:
        page = await self.memory.search(query, mode="hybrid", kind=kind, limit=limit)
        hits = [
            {
                "kind": hit.hit.kind,
                "source": source_of(hit.text),
                "artifact_id": hit.hit.artifact_ref.artifact_id,
                "revision": hit.hit.artifact_ref.revision,
                "score": round(float(hit.hit.score), 6),
                "matched_by": list(hit.matched_by),
                "text": hit.text,
            }
            for hit in page.hits
        ]
        append_jsonl(
            self.paths.records / "db-queries.jsonl",
            {
                "at": utc_now(),
                "scope_id": self.scope_id,
                "operation": "search",
                "mode": page.mode,
                "kind": kind,
                "query": query,
                "limit": limit,
                "returned": len(hits),
                "hits": [
                    {
                        "source": item["source"],
                        "artifact_id": item["artifact_id"],
                        "score": item["score"],
                        "matched_by": item["matched_by"],
                    }
                    for item in hits
                ],
            },
        )
        return hits


@asynccontextmanager
async def open_store(paths: Paths, *, reset: bool = False) -> AsyncIterator[Store]:
    paths.ensure()
    if reset:
        shutil.rmtree(paths.db, ignore_errors=True)
    config = BuiltinConfig(database=SeekDBConfig(path=paths.db))
    async with open_builtin_runtime(config, embedding_model=LocalHashEmbedder()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title=SCOPE_TITLE, summary=SCOPE_SUMMARY, idempotency_key=scope_key())
        )
        yield Store(runtime, scope.scope_id, paths)
