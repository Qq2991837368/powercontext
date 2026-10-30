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

"""Cross-call Embedding request coalescing with a bounded vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

Vector = tuple[float, ...]


@dataclass
class _Batch:
    """One underlying provider request shared by every waiter of its texts."""

    texts: tuple[str, ...]
    usage: InferenceUsage | None = None


@dataclass
class _Ticket:
    """One in-flight text: the shared result and the batch that will resolve it."""

    future: asyncio.Future[Vector]
    batch: _Batch | None = None


@dataclass
class _Result:
    vectors: dict[str, Vector] = field(default_factory=dict)


class CoalescingEmbeddingModel:
    """Share in-flight Embedding work across calls and cache successful vectors."""

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003

        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, Vector] = OrderedDict()
        self._inflight: dict[str, _Ticket] = {}
        self._pending: list[str] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: Any) -> EmbeddingResult:
        """Embed texts once each and return vectors in the caller's original order."""

        requested = list(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique: list[str] = []
        seen: set[str] = set()
        for text in requested:
            if text not in seen:
                seen.add(text)
                unique.append(text)

        resolved = _Result()
        waiting: dict[str, _Ticket] = {}
        for text in unique:
            cached = self._cache_get(text)
            if cached is not None:
                resolved.vectors[text] = cached
                continue
            ticket = self._inflight.get(text)
            if ticket is None:
                ticket = _Ticket(future=asyncio.get_running_loop().create_future())
                self._inflight[text] = ticket
                self._pending.append(text)
                self._schedule_flush()
            waiting[text] = ticket

        if waiting:
            results = await asyncio.gather(*(asyncio.shield(ticket.future) for ticket in waiting.values()))
            for text, vector in zip(waiting, results):
                resolved.vectors[text] = vector

        return EmbeddingResult(
            vectors=tuple(resolved.vectors[text] for text in requested),
            usage=_sum_usage(
                {
                    id(ticket.batch): ticket.batch
                    for ticket in waiting.values()
                    if ticket.batch is not None
                }.values()
            ),
        )

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        if self._coalesce_seconds <= 0:
            self._flush_handle = loop.call_soon(self._start_flush)
        else:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._start_flush)

    def _start_flush(self) -> None:
        self._flush_handle = None
        if not self._pending:
            return
        task = asyncio.ensure_future(self._drain())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self) -> None:
        """Issue every pending text once, split into bounded provider batches."""

        pending = self._pending
        self._pending = []
        batches = [
            tuple(pending[index : index + self._max_batch_size])
            for index in range(0, len(pending), self._max_batch_size)
        ]
        await asyncio.gather(
            *(self._run_batch(texts) for texts in batches),
            return_exceptions=True,
        )

    async def _run_batch(self, texts: tuple[str, ...]) -> None:
        batch = _Batch(texts=texts)
        for text in texts:
            ticket = self._inflight.get(text)
            if ticket is not None:
                ticket.batch = batch
        try:
            result = await self._model.embed(texts)
            vectors = tuple(tuple(float(value) for value in vector) for vector in result.vectors)
            if len(vectors) != len(texts):
                raise ValueError(  # noqa: TRY003
                    "provider returned a vector count that does not match the batch"
                )
        except BaseException as error:  # noqa: BLE001 - delivered to every waiter of this batch
            for text in texts:
                ticket = self._inflight.pop(text, None)
                if ticket is not None and not ticket.future.done():
                    ticket.future.set_exception(error)
                    ticket.future.add_done_callback(_silence_future)
            return

        batch.usage = result.usage
        for text, vector in zip(texts, vectors):
            self._cache_put(text, vector)
            ticket = self._inflight.pop(text, None)
            if ticket is not None and not ticket.future.done():
                ticket.future.set_result(vector)

    def _cache_get(self, text: str) -> Vector | None:
        if not self._cache_size:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _cache_put(self, text: str, vector: Vector) -> None:
        if not self._cache_size:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _silence_future(future: asyncio.Future[Any]) -> None:
    """Retrieve an exception that a cancelled waiter never collects."""

    if not future.cancelled():
        future.exception()


def _sum_usage(batches: Any) -> InferenceUsage:
    totals = [batch.usage for batch in batches if batch is not None and batch.usage is not None]
    if not totals:
        return InferenceUsage(requests=0)
    requests = sum(item.requests for item in totals)
    inputs = [item.input_tokens for item in totals]
    outputs = [item.output_tokens for item in totals]
    return InferenceUsage(
        requests=requests,
        input_tokens=None if all(value is None for value in inputs) else sum(value or 0 for value in inputs),
        output_tokens=None if all(value is None for value in outputs) else sum(value or 0 for value in outputs),
    )


__all__ = ["CoalescingEmbeddingModel"]
