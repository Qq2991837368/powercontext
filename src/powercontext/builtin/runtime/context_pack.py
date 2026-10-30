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

"""Boundary-safe Context Pack serialization with prefix-reusing deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(Exception):
    """A Context Pack or Context Pack Delta violates the wire contract."""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("items must be a list")
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("items must contain base64 strings")
        try:
            decoded.append(base64.b64decode(entry, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("items contain invalid base64") from error
    return tuple(decoded)


def _digest(items: tuple[bytes, ...]) -> str:
    canonical = _canonical({"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)})
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _pack_payload(items: tuple[bytes, ...], digest: str, byte_count: int) -> dict[str, Any]:
    return {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": _encode_items(items),
        "digest": digest,
        "byte_count": byte_count,
    }


def _settled_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    """Find the byte count that equals the serialized length of its own payload."""

    byte_count = 0
    for _attempt in range(8):
        length = len(_canonical(_pack_payload(items, digest, byte_count)))
        if length == byte_count:
            return byte_count
        byte_count = length
    raise ContextPackError("byte_count did not settle")


def _load_object(data: bytes, fields: frozenset[str]) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("payload is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload must be a JSON object")
    if set(payload) != fields:
        raise ContextPackError("payload does not declare exactly the contract fields")
    return payload


@dataclass(frozen=True)
class ContextPack:
    """One complete, independently verifiable context payload."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        """Build a self-consistent pack from ordered byte chunks."""

        resolved = tuple(bytes(item) for item in items)
        digest = _digest(resolved)
        return cls(items=resolved, digest=digest, byte_count=_settled_byte_count(resolved, digest))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split text into deterministic UTF-8 chunks that never exceed the caller limit."""

        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        chunks: list[bytes] = []
        current: list[str] = []
        size = 0
        for character in content:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk_bytes is too small for one UTF-8 character")
            if size + len(encoded) > chunk_bytes:
                chunks.append("".join(current).encode("utf-8"))
                current = []
                size = 0
            current.append(character)
            size += len(encoded)
        if current:
            chunks.append("".join(current).encode("utf-8"))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        """Serialize the canonical wire payload, including its own byte count."""

        payload = _pack_payload(self.items, self.digest, self.byte_count)
        encoded = _canonical(payload)
        if len(encoded) != self.byte_count:
            raise ContextPackError("byte_count does not match the serialized payload")
        return encoded

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        """Decode and verify one complete wire payload."""

        payload = _load_object(data, _PACK_FIELDS)
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("unsupported context pack schema")
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not isinstance(digest, str) or digest != _digest(items):
            raise ContextPackError("context pack digest does not match its items")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ContextPackError("byte_count must be an integer")
        if byte_count != len(data):
            raise ContextPackError("byte_count does not match the payload length")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """One prefix-reusing delta that rebuilds a target pack from a base pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Keep the longest shared chunk prefix and carry the target suffix."""

        limit = min(len(base.items), len(target.items))
        prefix = 0
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=tuple(target.items[prefix:]),
        )

    @property
    def byte_count(self) -> int:
        """Actual protocol bytes, excluding this observational property."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the canonical delta payload."""

        return _canonical(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        """Decode and structurally verify one wire delta."""

        payload = _load_object(data, _DELTA_FIELDS)
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("unsupported context pack delta schema")
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not isinstance(base_digest, str) or not isinstance(target_digest, str):
            raise ContextPackError("delta digests must be strings")
        prefix_items = payload["prefix_items"]
        if isinstance(prefix_items, bool) or not isinstance(prefix_items, int) or prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild and verify the target pack from a base pack and one delta."""

    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("rebuilt context does not match the delta target digest")
    return rebuilt


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
