"""Deterministic local embedding model.

The demo runs offline, so the vector channel uses a reproducible hashed
bag-of-tokens embedding instead of a paid embedding endpoint. The profile is
fixed at 256 dimensions because PowerContext binds the vector index to the
embedding profile; changing the dimension requires rebuilding the database.
"""

from __future__ import annotations

import hashlib
import math

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage


class LocalHashEmbedder:
    profile = EmbeddingProfile(
        profile_id="local-hash-256",
        model="local-hash",
        dimension=256,
        distance="l2",
        normalization="unit",
    )

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple(self._vector(text) for text in texts),
            usage=InferenceUsage(requests=1, input_tokens=sum(len(text.split()) for text in texts)),
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        buckets = [0.0] * self.profile.dimension
        lowered = text.lower()
        tokens = lowered.split()
        tokens.extend(lowered[i : i + 2] for i in range(max(0, len(lowered) - 1)))
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.profile.dimension
            buckets[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in buckets)) or 1.0
        return tuple(value / norm for value in buckets)
