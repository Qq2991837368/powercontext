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

"""HTTP cross-encoder reranking for coarse Memory search candidates."""

from __future__ import annotations

import math

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class _StrictModel(BaseModel):
    """Reject any field the offline scoring protocol does not define."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _RerankCandidate(_StrictModel):
    """One candidate addressed by its rank in the complete input pool."""

    rank: int = Field(ge=1)
    text: str


class _RerankScore(_StrictModel):
    """One finite score for one complete-pool rank."""

    rank: int = Field(ge=1)
    score: float

    @field_validator("score")
    @classmethod
    def require_finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be a finite number")  # noqa: TRY003
        return value


class _RerankResponse(_StrictModel):
    """The offline scoring response: exactly one ``scores`` collection."""

    scores: tuple[_RerankScore, ...]


class HTTPMemoryReranker:
    """Score Memory candidates with one HTTP service and select globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")  # noqa: TRY003
        if not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Batch every candidate once, then select from the fused global scores."""

        if not candidates:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > len(candidates):
            raise ValueError(  # noqa: TRY003
                f"limit must be between 1 and {len(candidates)}, got {limit}"
            )

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            ranks = tuple(range(start + 1, min(start + self._batch_size, len(candidates)) + 1))
            payload = {
                "query": query,
                "candidates": [
                    _RerankCandidate(rank=rank, text=candidates[rank - 1].text).model_dump()
                    for rank in ranks
                ],
            }
            response = await self._client.post(self._endpoint, json=payload)
            requests += 1
            response.raise_for_status()
            scores.update(self._parse_scores(_read_json(response), ranks))

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _score in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )

    @staticmethod
    def _parse_scores(payload: object, ranks: tuple[int, ...]) -> dict[int, float]:
        """Validate that one batch response covers exactly the ranks it was sent."""

        parsed = _RerankResponse.model_validate(payload)
        expected = set(ranks)
        scored: dict[int, float] = {}
        for item in parsed.scores:
            if item.rank not in expected:
                raise ValueError(  # noqa: TRY003
                    f"response reported rank {item.rank} outside the requested batch"
                )
            if item.rank in scored:
                raise ValueError(f"response reported rank {item.rank} more than once")  # noqa: TRY003
            scored[item.rank] = item.score
        missing = expected.difference(scored)
        if missing:
            raise ValueError(f"response omitted ranks {sorted(missing)}")  # noqa: TRY003
        return scored


def _read_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as error:
        raise ValueError("rerank response is not valid JSON") from error  # noqa: TRY003
