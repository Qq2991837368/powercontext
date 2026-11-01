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

"""Evidence-dependency lifecycle for Sources, Memory entries and Topic Memory."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.sources import SourceRef

NodeKind = Literal["source", "memory", "topic"]
Policy = Literal["all", "any"]
Purpose = Literal["generation", "audit"]

SOURCE_KIND: NodeKind = "source"
MEMORY_KIND: NodeKind = "memory"
TOPIC_KIND: NodeKind = "topic"


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event or resolution violates the contract."""


_NonBlank = Annotated[str, Field(min_length=1)]
_Positive = Annotated[int, Field(strict=True, gt=0)]
_Flag = Annotated[bool, Field(strict=True)]


class SourceLifecycleEvent(BaseModel):
    """One caller-declared availability change for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: _NonBlank
    reason: _NonBlank
    revision: _Positive
    sequence: _Positive
    available: _Flag

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class LifecycleNode:
    """One registered node and its derived availability."""

    node_id: str
    kind: NodeKind
    parents: tuple[str, ...]
    policy: Policy
    available: bool
    revision: int = 1
    sequence: int = 0
    fingerprint: str = ""


def _text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise SourceLifecycleError(f"{field} must be a non-empty trimmed string")
    return value


def _kind(value: object) -> NodeKind:
    if value not in (SOURCE_KIND, MEMORY_KIND, TOPIC_KIND):
        raise SourceLifecycleError("kind must be source, memory or topic")
    return value  # type: ignore[return-value]


def _policy(value: object) -> Policy:
    if value not in ("all", "any"):
        raise SourceLifecycleError("policy must be all or any")
    return value  # type: ignore[return-value]


def _fingerprint(event: SourceLifecycleEvent) -> str:
    return f"{event.available}|{event.reason}"


def _payload_identity(payload: dict[str, object]) -> tuple[object, ...]:
    kind = payload.get("kind")
    data = payload.get("data")
    values = data if isinstance(data, dict) else {}
    if kind == SOURCE_KIND:
        return (kind, values.get("source_type"), values.get("source_id"))
    if kind == MEMORY_KIND:
        return (kind, values.get("entry_id"), values.get("entry_version_id"))
    return (kind, values.get("family"), values.get("artifact_id"), values.get("revision"))


def _require_same_payload(
    node_id: str, payload: dict[str, object], payloads: dict[str, dict[str, object]]
) -> None:
    existing = payloads.get(node_id)
    if existing is not None and _payload_identity(existing) != _payload_identity(payload):
        raise SourceLifecycleError(f"node {node_id!r} is already registered with a different binding")


class LifecycleGraph:
    """In-memory dependency graph with all/any availability propagation."""

    def __init__(self) -> None:
        self._nodes: dict[str, LifecycleNode] = {}

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def node(self, node_id: str) -> LifecycleNode:
        try:
            return self._nodes[node_id]
        except KeyError as error:
            raise SourceLifecycleError(f"unknown lifecycle node {node_id!r}") from error

    @classmethod
    def restore(cls, nodes: object) -> "LifecycleGraph":
        graph = cls()
        for node in nodes:
            graph._nodes[node.node_id] = node
        return graph

    def nodes(self) -> tuple["LifecycleNode", ...]:
        return tuple(self._nodes.values())

    def active(self) -> list[str]:
        """Registered nodes that currently support generation, sorted by identity."""

        return sorted(node_id for node_id, node in self._nodes.items() if node.available)

    def register_source(self, node_id: object, *, revision: object = 1) -> LifecycleNode:
        node_id = _text("node_id", node_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        candidate = LifecycleNode(
            node_id=node_id,
            kind=SOURCE_KIND,
            parents=(),
            policy="all",
            available=True,
            revision=revision,
        )
        return self._register(candidate)

    def register_derived(
        self,
        node_id: object,
        parents: object,
        *,
        policy: object,
        kind: object,
    ) -> LifecycleNode:
        node_id = _text("node_id", node_id)
        resolved_kind = _kind(kind)
        resolved_policy = _policy(policy)
        if resolved_kind == SOURCE_KIND:
            raise SourceLifecycleError("derived nodes must be memory or topic")
        if not isinstance(parents, (tuple, list)) or not parents:
            raise SourceLifecycleError("derived nodes require at least one parent")
        resolved: list[str] = []
        for parent in parents:
            parent_id = _text("parent", parent)
            if parent_id == node_id:
                raise SourceLifecycleError("a node cannot depend on itself")
            if parent_id in resolved:
                raise SourceLifecycleError(f"duplicate parent {parent_id!r}")
            if parent_id not in self._nodes:
                raise SourceLifecycleError(f"parent {parent_id!r} is not registered yet")
            resolved.append(parent_id)
        candidate = LifecycleNode(
            node_id=node_id,
            kind=resolved_kind,
            parents=tuple(resolved),
            policy=resolved_policy,
            available=self._evaluate(tuple(resolved), resolved_policy),
        )
        return self._register(candidate)

    def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply one event and return every currently supporting node, sorted."""

        node = self.node(event.node_id)
        if node.kind != SOURCE_KIND:
            raise SourceLifecycleError("events only apply to registered Sources")
        if event.revision != node.revision:
            raise SourceLifecycleError("event revision does not match the Source revision")
        fingerprint = _fingerprint(event)
        if event.sequence == node.sequence:
            if fingerprint != node.fingerprint:
                raise SourceLifecycleError("event sequence was reused with different content")
            return self.active()
        if event.sequence < node.sequence:
            raise SourceLifecycleError("event sequence must increase monotonically")

        self._nodes[node.node_id] = replace(
            node,
            available=event.available,
            sequence=event.sequence,
            fingerprint=fingerprint,
        )
        self._propagate()
        return self.active()

    def _register(self, candidate: LifecycleNode) -> LifecycleNode:
        existing = self._nodes.get(candidate.node_id)
        if existing is not None:
            if self._binding(existing) != self._binding(candidate):
                raise SourceLifecycleError(  # noqa: TRY003
                    f"node {candidate.node_id!r} is already registered with a different binding"
                )
            return existing
        self._nodes[candidate.node_id] = candidate
        return candidate

    @staticmethod
    def _binding(node: LifecycleNode) -> tuple[object, ...]:
        return (node.kind, node.parents, node.policy, node.revision)

    def _evaluate(self, parents: tuple[str, ...], policy: Policy) -> bool:
        values = [self._nodes[parent].available for parent in parents]
        return all(values) if policy == "all" else any(values)

    def _propagate(self) -> None:
        changed = True
        while changed:
            changed = False
            for node_id, node in tuple(self._nodes.items()):
                if node.kind == SOURCE_KIND:
                    continue
                support = [self._nodes[parent].available for parent in node.parents]
                available = all(support) if node.policy == "all" else any(support)
                if available != node.available:
                    self._nodes[node_id] = replace(node, available=available)
                    changed = True


import json

from sqlalchemy import delete, insert, select

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE

_KIND_MODELS = {
    SOURCE_KIND: ("powercontext.sources", "SourceRef"),
    MEMORY_KIND: ("powercontext.builtin.artifacts.memory", "MemoryCitation"),
    TOPIC_KIND: ("powercontext.artifacts", "ArtifactRef"),
}


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str) -> object:
    return json.loads(value)


def _model_for(kind: str):
    import importlib

    module_name, attribute = _KIND_MODELS[kind]
    return getattr(importlib.import_module(module_name), attribute)


class SourceLifecycleApplication:
    """Expose the per-scope lifecycle service from one Runtime."""

    def __init__(self, runtime: object, database: object | None = None) -> None:
        self._runtime = runtime
        self._database = database

    async def unavailable_topic_artifact_ids(self, scope_id: str, /) -> frozenset[str]:
        """Topic artifacts whose lifecycle node currently withholds support."""

        if self._database is None:
            return frozenset()
        table = SOURCE_LIFECYCLE_TABLE
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(table.c.payload).where(
                        table.c.scope_id == scope_id,
                        table.c.kind == TOPIC_KIND,
                        table.c.available.is_(False),
                    )
                )
            ).all()
        blocked: set[str] = set()
        for row in rows:
            data = _loads(row.payload).get("data", {})
            artifact_id = data.get("artifact_id")
            if artifact_id:
                blocked.add(artifact_id)
        return frozenset(blocked)
    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        if self._database is None:
            raise SourceLifecycleError("source lifecycle requires a relational database")
        return ScopedSourceLifecycleApplication(self._runtime, self._database, scope_id)


class ScopedSourceLifecycleApplication:
    """Register evidence dependencies and propagate Source availability."""

    def __init__(self, runtime: object, database: object, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = _text("scope_id", scope_id)

    async def register_source(self, node_id: object, source_ref: object, *, revision: object = 1) -> None:
        reference = SourceRef.model_validate(source_ref)
        await self._require_source(reference)
        key = _text("node_id", node_id)
        payload = {"kind": SOURCE_KIND, "data": reference.model_dump(mode="json")}
        graph, payloads = await self._read()
        _require_same_payload(key, payload, payloads)
        payloads[key] = payload
        graph.register_source(key, revision=revision)
        await self._persist(graph, payloads)

    async def register_memory(
        self, node_id: object, citation: object, *, parents: object, policy: object
    ) -> None:
        reference = MemoryCitation.model_validate(citation)
        await self._require_memory(reference)
        await self._register_derived(node_id, reference, parents, policy, MEMORY_KIND)

    async def register_topic(
        self, node_id: object, reference: object, *, parents: object, policy: object
    ) -> None:
        topic = ArtifactRef.model_validate(reference)
        if topic.family != "topic-memory":
            raise SourceLifecycleError("topic nodes require a Topic Memory ArtifactRef")
        await self._require_topic(topic)
        await self._register_derived(node_id, topic, parents, policy, TOPIC_KIND)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        graph, payloads = await self._read()
        active = graph.apply(event)
        await self._persist(graph, payloads)
        await self._sync_memory(graph, payloads, event.reason)
        return active

    async def resolve(self, node_id: object, *, purpose: object = "generation") -> object:
        graph, payloads = await self._read()
        node = graph.node(_text("node_id", node_id))
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("purpose must be generation or audit")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node {node.node_id!r} does not support generation")
        return _model_for(node.kind).model_validate(payloads[node.node_id]["data"])

    async def _register_derived(
        self, node_id: object, reference: object, parents: object, policy: object, kind: str
    ) -> None:
        key = _text("node_id", node_id)
        payload = {"kind": kind, "data": reference.model_dump(mode="json")}
        graph, payloads = await self._read()
        _require_same_payload(key, payload, payloads)
        payloads[key] = payload
        node = graph.register_derived(key, parents, policy=policy, kind=kind)
        await self._persist(graph, payloads)
        if kind == MEMORY_KIND and not node.available:
            # Registration itself must honour the parents' current availability.
            await self._apply_memory_state(reference, active=False, reason="registered without support")

    async def _require_source(self, reference: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                reference.source_type, reference.source_id
            )
        except (LookupError, ValueError) as error:
            raise SourceLifecycleError(f"source reference is not readable: {reference.source_id!r}") from error

    async def _require_memory(self, citation: MemoryCitation) -> None:
        from powercontext.builtin.runtime import GetMemoryEntryRequest

        memory = self._runtime.memory.for_scope(self.scope_id)
        try:
            await memory.get(GetMemoryEntryRequest(citation=citation))
        except (LookupError, ValueError) as error:
            raise SourceLifecycleError(f"memory citation is not readable: {citation.entry_id!r}") from error

    async def _require_topic(self, reference: ArtifactRef) -> None:
        records = self._runtime.records.for_scope(self.scope_id)
        try:
            await records.get_artifact_revision(reference.family, reference.artifact_id, reference.revision)
        except (LookupError, ValueError) as error:
            raise SourceLifecycleError(f"topic reference is not readable: {reference.artifact_id!r}") from error

    async def _read(self) -> tuple[LifecycleGraph, dict[str, dict[str, object]]]:
        table = SOURCE_LIFECYCLE_TABLE
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(select(table).where(table.c.scope_id == self.scope_id))
            ).all()
        nodes = [
            LifecycleNode(
                node_id=row.node_id,
                kind=row.kind,
                parents=tuple(_loads(row.parents)),
                policy=row.policy,
                available=bool(row.available),
                revision=int(row.revision),
                sequence=int(row.sequence),
                fingerprint=row.fingerprint,
            )
            for row in rows
        ]
        payloads = {row.node_id: _loads(row.payload) for row in rows}
        return LifecycleGraph.restore(nodes), payloads

    async def _persist(self, graph: LifecycleGraph, payloads: dict[str, dict[str, object]]) -> None:
        table = SOURCE_LIFECYCLE_TABLE
        async with self._database.transaction() as connection:
            await connection.execute(delete(table).where(table.c.scope_id == self.scope_id))
            for node in graph.nodes():
                await connection.execute(
                    insert(table).values(
                        scope_id=self.scope_id,
                        node_id=node.node_id,
                        kind=node.kind,
                        payload=_dumps(payloads.get(node.node_id, {})),
                        parents=_dumps(list(node.parents)),
                        policy=node.policy,
                        available=node.available,
                        revision=node.revision,
                        sequence=node.sequence,
                        fingerprint=node.fingerprint,
                    )
                )

    async def _sync_memory(
        self,
        graph: LifecycleGraph,
        payloads: dict[str, dict[str, object]],
        reason: str,
    ) -> None:
        # Reconcile every Memory node against the graph. Applying the same target
        # state twice is a no-op, so a retried event finishes an interrupted
        # propagation without adding Memory revisions on a successful replay.
        for node in graph.nodes():
            if node.kind != MEMORY_KIND:
                continue
            citation = _model_for(MEMORY_KIND).model_validate(payloads[node.node_id]["data"])
            await self._apply_memory_state(citation, active=node.available, reason=reason)

    async def _apply_memory_state(self, citation: object, *, active: bool, reason: str) -> None:
        from powercontext.builtin.runtime.application import _current_citation

        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                service = context.artifacts.memory
                memory_artifact_id = context.artifacts.memory_artifact_id
                head = await service.head(memory_artifact_id)
                # The registered citation pins the entry version; the memory artifact itself
                # advances with every write, so resolve the entry against the current head.
                fresh = citation.model_copy(update={"memory_ref": head.as_ref()})
                current, entry = await _current_citation(service, memory_artifact_id, fresh)
                if active:
                    await service.reactivate(current, entries=(entry,), reason=reason)
                else:
                    await service.forget(current, entries=(entry,), reason=reason)

__all__ = [
    "LifecycleGraph",
    "LifecycleNode",
    "SourceLifecycleError",
    "ScopedSourceLifecycleApplication",
    "SourceLifecycleApplication",
    "SourceLifecycleEvent",
]







