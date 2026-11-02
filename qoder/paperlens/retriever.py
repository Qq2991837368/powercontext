"""PaperLens 混合检索：多路召回、RRF 融合、去重与重排。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .planner import QueryPlan, find_terms

_SEARCH_LIMIT = 40
_RRF_K = 60
_SOURCE_LINE = re.compile(r"^\[\[SOURCE\s+(.+?)\]\]$")
_HEADING_PREFIX = "## "
_HEADING_NUMBER = re.compile(r"^\d+\.\s*")
_SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")


class SearchStore(Protocol):
    """``retrieve`` 所需的最小 store 接口。"""

    async def search(
        self, query: str, *, kind: str, limit: int
    ) -> list[dict[str, Any]]:
        """按查询返回指定类型的前 ``limit`` 条结果。"""
        ...


@dataclass(frozen=True)
class Hit:
    """检索并重排后的命中块。"""

    chunk_id: str
    paper_id: str
    section: str
    text: str
    artifact_id: str
    score: float
    rrf: float
    matched_by: tuple[str, ...]
    terms: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass
class _Candidate:
    """RRF 融合阶段的内部候选。"""

    artifact_id: str
    text: str
    paper_id: str
    section: str
    body: str
    rrf: float = 0.0
    matched_by: tuple[str, ...] = ()


async def retrieve(
    store: SearchStore,
    plan: QueryPlan,
    *,
    top: int,
    per_paper: int = 2,
    exclude_papers: Iterable[str] = (),
) -> list[Hit]:
    """执行多路检索、融合、去重与重排，返回前 ``top`` 条命中。"""
    if top <= 0 or per_paper <= 0:
        return []

    excluded = {paper_id.strip() for paper_id in exclude_papers if paper_id.strip()}
    candidates: dict[str, _Candidate] = {}

    for variant in plan.variants:
        query = variant.strip()
        if not query:
            continue
        results = await store.search(query, kind="paper-chunk", limit=_SEARCH_LIMIT)
        seen_in_variant: set[str] = set()
        for rank, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind is not None and kind != "paper-chunk":
                continue
            text = str(item.get("text") or "")
            if not text.strip():
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not artifact_id:
                artifact_id = _digest(text)
            if artifact_id in seen_in_variant:
                continue
            seen_in_variant.add(artifact_id)

            candidate = candidates.get(artifact_id)
            if candidate is None:
                paper_id, section, body = _parse_chunk(text, artifact_id)
                candidate = _Candidate(
                    artifact_id=artifact_id,
                    text=text,
                    paper_id=paper_id,
                    section=section,
                    body=body,
                )
                candidates[artifact_id] = candidate
            candidate.rrf += 1.0 / (_RRF_K + rank)
            candidate.matched_by = _merge_matched_by(
                candidate.matched_by,
                _normalize_matched_by(item.get("matched_by")),
            )

    deduplicated = _deduplicate(candidates.values(), excluded)
    retained = _cap_per_paper(deduplicated, per_paper)
    if not retained:
        return []

    planned_terms = _unique_terms(plan.terms)
    max_rrf = max(candidate.rrf for candidate in retained)
    hits: list[Hit] = []
    for candidate in retained:
        terms = find_terms(candidate.text, planned_terms)
        coverage = len(terms) / len(planned_terms) if planned_terms else 0.0
        heading = 1.0 if terms and _contains_any(candidate.section, terms) else 0.0
        normalized = candidate.rrf / max_rrf if max_rrf > 0 else 0.0
        score = round(0.65 * normalized + 0.30 * coverage + 0.05 * heading, 4)
        hits.append(Hit(
            chunk_id=_digest(candidate.text),
            paper_id=candidate.paper_id,
            section=candidate.section,
            text=candidate.text,
            artifact_id=candidate.artifact_id,
            score=score,
            rrf=candidate.rrf,
            matched_by=candidate.matched_by,
            terms=terms,
            evidence=_select_evidence(candidate.body, planned_terms),
        ))

    hits.sort(key=lambda hit: (-hit.score, -hit.rrf, hit.chunk_id))
    return hits[:top]


def _digest(text: str) -> str:
    """返回文本 SHA-256 的前 12 位，作为稳定的块标识。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _parse_chunk(text: str, artifact_id: str) -> tuple[str, str, str]:
    """从块文本中解析论文 id、小节标题与正文。"""
    lines = text.splitlines()
    paper_id = ""
    body_start = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        match = _SOURCE_LINE.match(line)
        if match is not None:
            paper_id = match.group(1).strip()
            body_start = index + 1
        break
    if not paper_id:
        paper_id = artifact_id

    section = ""
    heading_index = -1
    for index in range(body_start, len(lines)):
        line = lines[index]
        if not line.startswith(_HEADING_PREFIX):
            continue
        raw_title = line[len(_HEADING_PREFIX):].strip()
        title = _HEADING_NUMBER.sub("", raw_title).strip()
        section = title or "Untitled"
        heading_index = index
        break

    start = body_start if heading_index < 0 else heading_index + 1
    body = "\n".join(lines[start:]).strip()
    return paper_id, section, body


def _deduplicate(
    candidates: Iterable[_Candidate], excluded: set[str]
) -> list[_Candidate]:
    """按论文排除，并让同一小节只保留融合分最高的候选。"""
    by_section: dict[tuple[str, str], _Candidate] = {}
    for candidate in candidates:
        if candidate.paper_id in excluded:
            continue
        # 无法解析小节时退化为按块去重，避免把不同块错误合并。
        section_key = candidate.section or f"#{candidate.artifact_id}"
        key = (candidate.paper_id, section_key)
        current = by_section.get(key)
        if current is None or candidate.rrf > current.rrf:
            by_section[key] = candidate
    return sorted(
        by_section.values(),
        key=lambda item: (-item.rrf, item.artifact_id),
    )


def _cap_per_paper(candidates: list[_Candidate], per_paper: int) -> list[_Candidate]:
    """按融合分顺序限制每篇论文的候选数量。"""
    counts: dict[str, int] = {}
    retained: list[_Candidate] = []
    for candidate in candidates:
        used = counts.get(candidate.paper_id, 0)
        if used >= per_paper:
            continue
        counts[candidate.paper_id] = used + 1
        retained.append(candidate)
    return retained


def _unique_terms(terms: Iterable[str]) -> tuple[str, ...]:
    """去空白并按大小写不敏感去重，保留首次出现的写法。"""
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    """判断文本是否包含任一条术语（大小写不敏感）。"""
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def _select_evidence(body: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """从正文中挑选最多三句可摘录证据。"""
    if not terms:
        return ()
    ranked: list[tuple[int, int, int, str]] = []
    for index, raw in enumerate(_SENTENCE_SPLIT.split(body)):
        sentence = raw.strip()
        if not 20 <= len(sentence) <= 220:
            continue
        hits = len(find_terms(sentence, terms))
        if hits == 0:
            continue
        ranked.append((-hits, len(sentence), index, sentence))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in ranked[:3])


def _normalize_matched_by(value: object) -> tuple[str, ...]:
    """把 store 返回的 matched_by 规范为去重元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        seen: set[str] = set()
        for entry in value:
            text = str(entry).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return tuple(result)
    text = str(value).strip()
    return (text,) if text else ()


def _merge_matched_by(
    existing: tuple[str, ...], extra: tuple[str, ...]
) -> tuple[str, ...]:
    """按首次出现顺序合并多个变体的 matched_by。"""
    if not extra:
        return existing
    merged = list(existing)
    seen = {value.casefold() for value in merged}
    for value in extra:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(value)
    return tuple(merged)
