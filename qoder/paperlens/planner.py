"""PaperLens 查询规划：领域术语识别、跨语言扩展与检索变体生成。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# 概念组按“具体优先”排列，避免宽泛术语抢先匹配后产生重复扩展。
TERM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("乘积量化", "PQ", "product quantization"),
    (
        "倒排",
        "倒排索引",
        "倒排表",
        "inverted file",
        "inverted index",
        "inverted list",
        "IVF",
    ),
    ("图索引", "graph index", "graph-based index", "HNSW", "NSW"),
    ("各向异性", "anisotropic", "anisotropy"),
    ("内积", "inner product", "MIPS", "maximum inner product search"),
    ("残差", "residual", "residual quantization"),
    ("码本", "codebook"),
    ("子空间", "subspace"),
    ("压缩率", "压缩", "compression", "compression ratio"),
    ("十亿级", "billion-scale", "billion scale", "billion"),
    ("召回率", "召回", "recall"),
    ("延迟", "时延", "latency"),
    ("内存", "memory"),
    ("磁盘", "SSD", "disk", "solid-state drive"),
    ("精排", "rerank", "re-rank", "re-ranking", "reranking"),
    ("粗筛", "粗检索", "coarse", "coarse search"),
    ("维度", "dimension", "dimensionality"),
    ("邻居", "neighbor", "neighbour", "nearest neighbor", "nearest neighbour"),
    ("构建", "construction", "index construction", "index building", "build"),
    (
        "自适应",
        "adaptive",
        "嵌套",
        "Matryoshka",
        "Matryoshka representation learning",
        "MRL",
    ),
    ("量化", "quantization", "quantize", "quantized", "quantizer"),
)

STOPWORDS: frozenset[str] = frozenset({
    # 中文疑问词与虚词
    "如何",
    "为什么",
    "怎样",
    "哪些",
    "什么",
    "应该",
    "可以",
    "是否",
    "以及",
    "我们",
    "这个",
    "那个",
    "之间",
    "的",
    "和",
    "与",
    "在",
    "对",
    "中",
    "之",
    "吗",
    "呢",
    "了",
    "是",
    "有",
    "把",
    "被",
    "先",
    "还",
    "才",
    "就",
    # 英文停用词
    "the",
    "and",
    "or",
    "of",
    "for",
    "to",
    "in",
    "on",
    "with",
    "how",
    "why",
    "what",
    "should",
    "which",
    "is",
    "are",
    "a",
    "an",
    "be",
    "been",
    "being",
    "by",
    "as",
    "at",
    "from",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "we",
    "you",
    "they",
    "do",
    "does",
    "did",
    "can",
    "could",
    "would",
    "will",
    "shall",
    "may",
    "might",
    "must",
    "about",
    "into",
    "than",
    "then",
    "there",
    "their",
    "was",
    "were",
    "has",
    "have",
    "had",
    "not",
    "no",
    "if",
    "so",
    "such",
    "also",
    "more",
    "most",
    "much",
    "very",
    "between",
    "during",
    "over",
    "under",
    "again",
    "further",
    "once",
    "here",
    "when",
    "where",
    "who",
    "whom",
    "whose",
    "all",
    "any",
    "both",
    "each",
    "few",
    "other",
    "some",
    "only",
    "own",
    "same",
    "too",
    "now",
    "up",
    "down",
    "out",
    "off",
})

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass(frozen=True)
class QueryPlan:
    """一次查询的检索计划。"""

    question: str
    terms: tuple[str, ...]
    expansions: tuple[str, ...]
    variants: tuple[str, ...]


def plan_query(question: str) -> QueryPlan:
    """把问题解析为领域术语、扩展词与最多三条检索变体。"""
    text = question.strip()
    folded = text.casefold()
    terms: list[str] = []
    seen_terms: set[str] = set()
    expansions: list[str] = []
    seen_expansions: set[str] = set()
    replacements: list[tuple[str, str]] = []
    seen_sources: set[str] = set()

    for group in TERM_GROUPS:
        present = [member for member in group if member and member.casefold() in folded]
        if not present:
            continue
        matched = max(present, key=len)
        _append_unique(terms, seen_terms, matched)

        chinese = [member for member in present if _contains_cjk(member)]
        english = _english_spelling(group)
        if chinese and english is not None:
            source = max(chinese, key=len)
            key = source.casefold()
            if key not in seen_sources:
                seen_sources.add(key)
                replacements.append((source, english))

        for member in group:
            if not member or member.casefold() in folded:
                continue
            _append_unique(expansions, seen_expansions, member)

    for token in _ASCII_WORD.findall(text):
        normalized = token.casefold()
        if len(normalized) < 3 or normalized in STOPWORDS:
            continue
        if normalized in seen_terms:
            continue
        _append_unique(terms, seen_terms, normalized)

    variants: list[str] = [text]
    if expansions:
        _append_variant(variants, " ".join([text, *expansions]))
    if replacements:
        replaced = _replace_spellings(text, replacements)
        if replaced != text:
            _append_variant(variants, replaced)
    return QueryPlan(
        question=text,
        terms=tuple(terms),
        expansions=tuple(expansions),
        variants=tuple(variants[:3]),
    )


def find_terms(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    """返回 ``terms`` 中在 ``text`` 内出现的项，保持传入顺序。"""
    folded = text.casefold()
    matched: list[str] = []
    for term in terms:
        if not term or not term.strip():
            continue
        if term.casefold() in folded:
            matched.append(term)
    return tuple(matched)


def _append_unique(items: list[str], seen: set[str], value: str) -> None:
    """按大小写不敏感去重后追加非空值。"""
    if not value:
        return
    key = value.casefold()
    if key in seen:
        return
    seen.add(key)
    items.append(value)


def _append_variant(variants: list[str], value: str) -> None:
    """追加非空且未出现过的检索变体。"""
    if value and value not in variants:
        variants.append(value)


def _contains_cjk(value: str) -> bool:
    """判断字符串是否包含中日韩统一表意文字。"""
    return any(0x4E00 <= ord(char) <= 0x9FFF for char in value)


def _english_spelling(group: tuple[str, ...]) -> str | None:
    """取概念组中的第一个纯 ASCII 写法。"""
    for member in group:
        if member and not _contains_cjk(member):
            return member
    return None


def _replace_spellings(text: str, replacements: list[tuple[str, str]]) -> str:
    """按最长优先把中文术语替换为对应英文写法。"""
    result = text
    ordered = sorted(replacements, key=lambda pair: len(pair[0]), reverse=True)
    for source, target in ordered:
        result = result.replace(source, target)
    return result
