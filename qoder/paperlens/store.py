"""PaperLens 的 PowerContext/SeekDB 存储层。"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from powercontext.builtin.artifacts.search import AdmissionFloor
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.runtime.models import AtomicMemoryContent
from powercontext.builtin.scope import ScopeDraft

from .config import Paths, SCOPE_SUMMARY, SCOPE_TITLE, scope_key
from .corpus import corpus_fingerprint, load_papers
from .embeddings import LocalHashEmbedder

__all__ = [
    "CHUNK_KIND",
    "MANIFEST_KIND",
    "META_KIND",
    "NOTE_KIND",
    "QUERY_KIND",
    "Store",
    "append_jsonl",
    "open_store",
    "source_of",
    "utc_now",
]

CHUNK_KIND = "paper-chunk"
META_KIND = "paper-meta"
MANIFEST_KIND = "corpus-manifest"
NOTE_KIND = "research-note"
QUERY_KIND = "query-log"

_PAGE_SIZE = 100
_SOURCE_PATTERN = re.compile(r"^\[\[SOURCE (?P<paper_id>[^\]\n]+)\]\]$")


def utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """把一条 JSON 记录追加到指定 JSONL 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")


def source_of(text: str) -> str | None:
    """解析文本首行的 ``[[SOURCE {paper_id}]]`` 标记。"""
    lines = text.splitlines()
    if not lines:
        return None
    match = _SOURCE_PATTERN.match(lines[0].strip())
    return match.group("paper_id") if match else None


def _int_or_zero(value: Any) -> int:
    """把值安全地转换为整数，无法转换时返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dump_json(payload: dict[str, Any]) -> str:
    """把字典序列化为稳定的 JSON 文本。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _load_json_object(text: str, *, source: str) -> dict[str, Any]:
    """解析 JSON 对象，失败时抛出带来源信息的 ``ValueError``。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: 内容不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{source}: JSON 顶层不是对象")
    return value


class Store:
    """封装某个 scope 下的论文检索与研究记忆读写。"""

    def __init__(self, *, paths: Paths, memory: Any, scope_id: str) -> None:
        """保存路径、作用域 memory 与 scope id。"""
        self._paths = paths
        self._memory = memory
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        """返回当前 PowerContext scope 的真实标识。"""
        return self._scope_id

    def _log(self, operation: str, kind: str, **extra: Any) -> None:
        """向 db-queries.jsonl 追加一条访问日志。"""
        payload: dict[str, Any] = {
            "at": utc_now(),
            "operation": operation,
            "kind": kind,
            "scope_id": self._scope_id,
        }
        payload.update(extra)
        append_jsonl(self._paths.records / "db-queries.jsonl", payload)

    async def _create(self, kind: str, text: str) -> str:
        """创建一条记忆并返回其 artifact id。"""
        result = await self._memory.create(
            (AtomicMemoryContent(kind=kind, text=text),)
        )
        return result.records[0].ref.artifact_id

    async def _list_all(self, kind: str) -> list[Any]:
        """循环翻页读取指定 kind 的全部记录。"""
        items: list[Any] = []
        cursor: Any = None
        while True:
            page = await self._memory.list(
                kind=kind,
                limit=_PAGE_SIZE,
                cursor=cursor,
            )
            items.extend(page.items)
            next_cursor = getattr(page, "next_cursor", None)
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        return items

    async def _forget_all(self, kind: str) -> None:
        """删除指定 kind 的全部记录。"""
        for item in await self._list_all(kind):
            await self._memory.forget(
                item.ref.artifact_id,
                expected_revision=item.ref.revision,
                expected_state_version=item.state.state_version,
            )

    async def _current_manifest(self) -> dict[str, Any] | None:
        """读取版本号最大的清单，不写查询日志。"""
        manifests: list[tuple[int, dict[str, Any], str]] = []
        for item in await self._list_all(MANIFEST_KIND):
            artifact_id = item.ref.artifact_id
            manifest = _load_json_object(
                item.artifact.content.text,
                source=f"manifest {artifact_id}",
            )
            manifests.append(
                (_int_or_zero(manifest.get("version")), manifest, artifact_id)
            )
        if not manifests:
            return None
        _, latest, artifact_id = max(manifests, key=lambda entry: entry[0])
        result = dict(latest)
        result["artifact_id"] = artifact_id
        if result.get("manifest_artifact_id") is None:
            result["manifest_artifact_id"] = artifact_id
        return result

    async def import_corpus(self, refresh: bool = False) -> dict[str, Any]:
        """导入语料并写入清单，可选地先清空旧的语料记录。"""
        papers = load_papers(self._paths.papers)
        fingerprint = corpus_fingerprint(papers)
        previous = await self._current_manifest()
        previous_version = (
            _int_or_zero(previous.get("version")) if previous is not None else 0
        )
        version = previous_version + 1

        if refresh:
            await self._forget_all(CHUNK_KIND)
            await self._forget_all(META_KIND)

        paper_entries: list[dict[str, Any]] = []
        chunk_total = 0

        for paper in papers:
            chunk_ids = [chunk.chunk_id for chunk in paper.chunks]
            chunk_total += len(chunk_ids)
            for chunk in paper.chunks:
                text = f"[[SOURCE {paper.meta.id}]]\n{chunk.text}"
                await self._create(CHUNK_KIND, text)
            meta_payload: dict[str, Any] = {
                "id": paper.meta.id,
                "title": paper.meta.title,
                "authors": paper.meta.authors,
                "year": paper.meta.year,
                "venue": paper.meta.venue,
                "tags": list(paper.meta.tags),
                "path": paper.path,
                "sha256": paper.sha256,
                "chunk_ids": chunk_ids,
                "chunk_count": len(paper.chunks),
            }
            await self._create(META_KIND, _dump_json(meta_payload))
            paper_entries.append(
                {
                    "id": paper.meta.id,
                    "path": paper.path,
                    "sha256": paper.sha256,
                    "chunk_count": len(paper.chunks),
                }
            )

        imported_at = utc_now()
        manifest_payload: dict[str, Any] = {
            "version": version,
            "imported_at": imported_at,
            "scope_id": self._scope_id,
            "corpus_fingerprint": fingerprint,
            "papers": paper_entries,
            "chunk_total": chunk_total,
            "paper_total": len(paper_entries),
            "manifest_artifact_id": None,
        }
        manifest_artifact_id = await self._create(
            MANIFEST_KIND,
            _dump_json(manifest_payload),
        )
        manifest_payload["manifest_artifact_id"] = manifest_artifact_id
        manifest_payload["artifact_id"] = manifest_artifact_id

        append_jsonl(
            self._paths.records / "corpus-imports.jsonl",
            {
                "at": imported_at,
                "version": version,
                "scope_id": self._scope_id,
                "corpus_fingerprint": fingerprint,
                "paper_total": len(paper_entries),
                "chunk_total": chunk_total,
                "manifest_artifact_id": manifest_artifact_id,
                "refresh": refresh,
            },
        )
        self._log("import_corpus", MANIFEST_KIND)
        return manifest_payload

    async def current_manifest(self) -> dict[str, Any] | None:
        """返回版本号最大的语料清单。"""
        manifest = await self._current_manifest()
        self._log("current_manifest", MANIFEST_KIND)
        return manifest

    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        """读取单条 artifact 的公开视图。"""
        item = await self._memory.get(artifact_id)
        text = item.artifact.content.text
        result: dict[str, Any] = {
            "kind": item.artifact.content.kind,
            "source": source_of(text),
            "artifact_id": item.ref.artifact_id,
            "revision": item.ref.revision,
            "text": text,
        }
        self._log("get_artifact", result["kind"])
        return result

    async def search(
        self,
        query: str,
        kind: str = CHUNK_KIND,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """在指定 kind 中执行混合检索。"""
        page = await self._memory.search(
            query,
            mode="hybrid",
            kind=kind,
            limit=limit,
            admission=AdmissionFloor(
                lexical_coverage=0.0,
                lexical_min_matched_terms=1,
                min_semantic_similarity=0.0,
            ),
        )
        results: list[dict[str, Any]] = []
        for hit in page.hits:
            artifact_ref = hit.hit.artifact_ref
            artifact_id = artifact_ref.artifact_id
            revision = getattr(artifact_ref, "revision", None)
            if revision is None:
                record = await self._memory.get(artifact_id)
                revision = record.ref.revision
            raw_matched_by = getattr(hit, "matched_by", None)
            if raw_matched_by is None:
                matched_by: list[str] = []
            elif isinstance(raw_matched_by, str):
                matched_by = [raw_matched_by]
            else:
                matched_by = [str(value) for value in raw_matched_by]
            text = hit.text
            results.append(
                {
                    "kind": hit.hit.kind,
                    "source": source_of(text),
                    "artifact_id": artifact_id,
                    "revision": revision,
                    "score": round(float(hit.hit.score), 6),
                    "matched_by": matched_by,
                    "text": text,
                }
            )
        self._log(
            "search",
            kind,
            query=query,
            limit=limit,
            returned=len(results),
        )
        return results

    async def create_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建一条研究笔记，seq 自动递增。"""
        max_seq = 0
        for item in await self._list_all(NOTE_KIND):
            note = _load_json_object(
                item.artifact.content.text,
                source=f"note {item.ref.artifact_id}",
            )
            max_seq = max(max_seq, _int_or_zero(note.get("seq")))
        data = dict(payload)
        data["seq"] = max_seq + 1
        data["created_at"] = utc_now()
        artifact_id = await self._create(NOTE_KIND, _dump_json(data))
        result = dict(data)
        result["artifact_id"] = artifact_id
        self._log("create_note", NOTE_KIND)
        return result

    async def list_notes(self) -> list[dict[str, Any]]:
        """按 seq 升序返回全部研究笔记。"""
        notes: list[dict[str, Any]] = []
        for item in await self._list_all(NOTE_KIND):
            artifact_id = item.ref.artifact_id
            note = _load_json_object(
                item.artifact.content.text,
                source=f"note {artifact_id}",
            )
            note["artifact_id"] = artifact_id
            notes.append(note)
        notes.sort(key=lambda note: _int_or_zero(note.get("seq")))
        self._log("list_notes", NOTE_KIND)
        return notes

    async def log_query(self, payload: dict[str, Any]) -> str:
        """写入一条查询日志并返回 artifact id。"""
        data = dict(payload)
        if "created_at" not in data:
            data["created_at"] = utc_now()
        artifact_id = await self._create(QUERY_KIND, _dump_json(data))
        self._log("log_query", QUERY_KIND)
        return artifact_id

    async def list_query_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        """按 created_at 倒序返回最近的查询日志。"""
        logs: list[dict[str, Any]] = []
        for item in await self._list_all(QUERY_KIND):
            entry = _load_json_object(
                item.artifact.content.text,
                source=f"query log {item.ref.artifact_id}",
            )
            logs.append(entry)
        logs.sort(
            key=lambda entry: str(entry.get("created_at", "")),
            reverse=True,
        )
        selected = logs[: max(limit, 0)]
        self._log("list_query_logs", QUERY_KIND)
        return selected


@asynccontextmanager
async def open_store(
    paths: Paths,
    reset: bool = False,
) -> AsyncIterator[Store]:
    """打开 PowerContext 运行时并生成 Store 上下文。"""
    if reset:
        shutil.rmtree(paths.db, ignore_errors=True)
    paths.ensure()
    config = BuiltinConfig(database=SeekDBConfig(path=paths.db))
    async with open_builtin_runtime(
        config,
        embedding_model=LocalHashEmbedder(),
    ) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(
                title=SCOPE_TITLE,
                summary=SCOPE_SUMMARY,
                idempotency_key=scope_key(),
            )
        )
        memory = runtime.atomic_memory.for_scope(scope.scope_id)
        yield Store(paths=paths, memory=memory, scope_id=scope.scope_id)
