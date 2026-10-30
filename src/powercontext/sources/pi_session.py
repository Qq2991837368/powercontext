# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture the selected branch of a Pi session as immutable event Sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_CONNECTOR_VERSION = "1"
_UTF8_BOM = b"\xef\xbb\xbf"
_SESSION_TYPE = "session"
_SESSION_VERSION = 3


class PiSessionInput(BaseModel):
    """One complete Pi session archive plus the branch end to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """The adapter-native value of one visible Pi event."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """One captured Pi event kept together with its complete input record."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the delimiter-safe, bounded identity of one captured Pi event."""

    for field, value in (("session_id", session_id), ("event_id", event_id)):
        if not isinstance(value, str) or not value or not value.strip():
            raise ValueError(f"{field} must be a non-empty, non-blank string")  # noqa: TRY003
    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"pi event source id must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse one Pi archive and return the visible events of the selected branch."""

    session_id, records = _read_archive(value.content)
    parents = _build_graph(records)
    leaf_id = value.leaf_id if value.leaf_id is not None else _last_node_id(records)
    if leaf_id not in parents:
        raise ValueError(f"leaf {leaf_id!r} is not present in the session archive")  # noqa: TRY003

    events: list[PiEventInput] = []
    for record in reversed(_ancestor_chain(parents, leaf_id)):
        rendered = _render_event(record)
        if rendered is None:
            continue
        role, body = rendered
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=record.get("parentId"),
                event_type=record["type"],
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


def _decode(content: bytes) -> str:
    if content.startswith(_UTF8_BOM):
        content = content[len(_UTF8_BOM) :]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003


def _read_archive(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    text = _decode(content)
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed Pi session JSON line: {error}") from error  # noqa: TRY003
        if not isinstance(parsed, dict):
            raise ValueError("each Pi session line must be a JSON object")  # noqa: TRY003
        records.append(parsed)

    if not records:
        raise ValueError("Pi session archive must contain a session header")  # noqa: TRY003
    header = records[0]
    if header.get("type") != _SESSION_TYPE or header.get("version") != _SESSION_VERSION:
        raise ValueError("Pi session header must declare type=session and version=3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header id must be a non-empty string")  # noqa: TRY003
    return session_id, records[1:]


def _build_graph(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index every record by id, rejecting duplicate ids and dangling parents."""

    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every Pi session record needs a non-empty id")  # noqa: TRY003
        if event_id in by_id:
            raise ValueError(f"duplicate Pi session record id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None and parent_id not in by_id:
            raise ValueError(  # noqa: TRY003
                f"record {event_id!r} references missing parent {parent_id!r}"
            )
        by_id[event_id] = record
    return by_id


def _last_node_id(records: list[dict[str, Any]]) -> str:
    if not records:
        raise ValueError("Pi session archive must contain at least one record besides the header")  # noqa: TRY003
    return records[-1]["id"]


def _ancestor_chain(parents: dict[str, dict[str, Any]], leaf_id: str) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("Pi session archive contains a parent cycle")  # noqa: TRY003
        seen.add(current)
        record = parents[current]
        chain.append(record)
        current = record.get("parentId")
    return chain


def _render_event(record: dict[str, Any]) -> tuple[str, str] | None:
    event_type = record.get("type")
    if event_type == "message":
        return _render_message(record.get("message"))
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
        return (str(event_type), body) if body else None
    if event_type == "custom_message":
        body = _visible_text(record.get("content"))
        return ("custom", body) if body else None
    return None


def _render_message(message: object) -> tuple[str, str] | None:
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role:
        return None
    if role == "toolResult":
        body = f"[tool_result {message.get('toolName')} {message.get('toolCallId')}] " + _visible_text(
            message.get("content")
        )
        return role, body
    if role == "bashExecution":
        body = f"$ {message.get('command')}\n{message.get('output')}"
        return role, body
    body = _message_body(message.get("content"))
    return (role, body) if body else None


def _message_body(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            arguments = json.dumps(
                block.get("arguments"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parts.append(f"[tool_call {block.get('name')} {block.get('id')}] {arguments}")
    return "\n".join(parts)


def _visible_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


class PiEventSourceAdapter:
    """Resolve and read one captured Pi event."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _checkpoint_state(checkpoint: JsonValue | None) -> dict[str, dict[str, str]]:
    """Read the per-session event digests already committed for this binding."""

    if not isinstance(checkpoint, dict):
        return {}
    sessions = checkpoint.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    state: dict[str, dict[str, str]] = {}
    for session_id, events in sessions.items():
        if isinstance(session_id, str) and isinstance(events, dict):
            state[session_id] = {
                str(event_id): str(digest) for event_id, digest in events.items() if isinstance(digest, str)
            }
    return state


def _event_digest(event: PiEventInput) -> str:
    payload = json.dumps(
        {"content": event.content, "record": event.record},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Import not-yet-captured visible events of one Pi session branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        events = parse_pi_session(PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id))
        state = _checkpoint_state(session.checkpoint)

        pending: list[PiEventInput] = []
        digests: dict[str, str] = {}
        for event in events:
            digest = _event_digest(event)
            known = state.get(event.session_id, {}).get(event.event_id)
            if known is not None:
                if known != digest:
                    raise ValueError(  # noqa: TRY003
                        f"captured Pi event {event.event_id!r} was rewritten"
                    )
                continue
            pending.append(event)
            digests[event.event_id] = digest

        for event in pending:
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)

        if pending:
            session_id = events[0].session_id if events else ""
            merged = dict(state)
            merged.setdefault(session_id, {}).update(digests)
            checkpoint: JsonValue = {"version": 1, "sessions": merged}
        else:
            checkpoint = session.checkpoint
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)
