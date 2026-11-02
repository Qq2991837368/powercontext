"""确定性的本地哈希嵌入实现。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage

PROFILE_ID = "local-hash-384"
DIMENSION = 384
ASCII_WEIGHT = 1.2
CJK_CHAR_WEIGHT = 0.45
CJK_BIGRAM_WEIGHT = 1.0
_ASCII_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")

PROFILE = EmbeddingProfile(
    profile_id=PROFILE_ID,
    model="local-hash",
    dimension=DIMENSION,
    distance="l2",
    normalization="unit",
)


def _bucket_and_sign(token: str) -> tuple[int, float]:
    """计算 token 的落桶位置与符号。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % DIMENSION, (1.0 if digest[0] % 2 == 0 else -1.0)


def _iter_weighted_tokens(text: str) -> Iterator[tuple[str, float]]:
    """按规则产出带权重的 ASCII/CJK token。"""
    lowered = text.lower()
    for match in _ASCII_PATTERN.finditer(lowered):
        yield match.group(0), ASCII_WEIGHT
    for match in _CJK_PATTERN.finditer(text):
        run = match.group(0)
        for char in run:
            yield char, CJK_CHAR_WEIGHT
        for index in range(len(run) - 1):
            yield run[index:index + 2], CJK_BIGRAM_WEIGHT


def _embed_text(text: str) -> tuple[tuple[float, ...], int]:
    """计算单条文本的向量与 token 数。"""
    vector = [0.0] * DIMENSION
    token_count = 0
    for token, weight in _iter_weighted_tokens(text):
        bucket, sign = _bucket_and_sign(token)
        vector[bucket] += sign * weight
        token_count += 1
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(0.0 for _ in range(DIMENSION)), token_count
    return tuple(value / norm for value in vector), token_count


class LocalHashEmbedder:
    """本地确定性哈希嵌入实现。"""

    profile: EmbeddingProfile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """批量计算嵌入向量。"""
        vectors: list[tuple[float, ...]] = []
        token_count = 0
        for text in texts:
            vector, count = _embed_text(text)
            vectors.append(vector)
            token_count += count
        return EmbeddingResult(
            vectors=tuple(vectors),
            usage=InferenceUsage(requests=1, input_tokens=token_count),
        )
