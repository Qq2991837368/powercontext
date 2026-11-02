"""PaperLens 证据包：构建、渲染与持久化。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Paths
from .corpus import Paper, PaperMeta, corpus_fingerprint
from .planner import QueryPlan, find_terms
from .retriever import Hit

_SOURCE_PREFIX = "[[SOURCE"
_HEADING_PREFIX = "## "
_SNIPPET_LIMIT = 160


def build_pack(
    question: str,
    plan: QueryPlan,
    hits: Sequence[Hit],
    *,
    papers: tuple[Paper, ...],
    memory_state: dict[str, Any],
    manifest: Any,
    scope_id: str,
) -> dict[str, Any]:
    """把检索结果、覆盖度与记忆状态打包为结构化证据包。

    覆盖度只依据 ``plan.terms`` 计算：某个术语出现在任一命中块的
    ``text`` 中即视为已覆盖。命中块的 ``snippet`` 优先使用
    ``Hit.evidence`` 的第一项，否则截取块正文前 160 个字符。

    Args:
        question: 本轮检索问题。
        plan: 查询计划。
        hits: 检索并重排后的命中块序列。
        papers: 语料中的全部论文。
        memory_state: 已折叠的研究记忆状态。
        manifest: 索引清单，支持对象属性或字典键访问。
        scope_id: 当前 scope 的标识。

    Returns:
        可直接序列化为 JSON 的证据包字典。
    """
    metas: dict[str, PaperMeta] = {paper.meta.id: paper.meta for paper in papers}
    terms = _unique_terms(plan.terms)
    covered_keys: set[str] = set()
    papers_hit: list[str] = []
    hit_paper_keys: set[str] = set()
    packed_hits: list[dict[str, Any]] = []

    for rank, hit in enumerate(hits, start=1):
        for term in find_terms(hit.text, terms):
            covered_keys.add(term.casefold())

        if hit.paper_id not in hit_paper_keys:
            hit_paper_keys.add(hit.paper_id)
            papers_hit.append(hit.paper_id)

        evidence = list(hit.evidence)
        snippet = evidence[0] if evidence else _body_excerpt(hit.text, _SNIPPET_LIMIT)
        packed_hits.append(
            {
                "rank": rank,
                "paper": _paper_payload(metas.get(hit.paper_id), hit.paper_id),
                "section": hit.section,
                "chunk_id": hit.chunk_id,
                "score": hit.score,
                "rrf": hit.rrf,
                "matched_by": list(hit.matched_by),
                "terms": list(hit.terms),
                "snippet": snippet,
                "evidence": evidence,
            }
        )

    terms_missing = [term for term in terms if term.casefold() not in covered_keys]
    terms_total = len(terms)
    terms_covered = terms_total - len(terms_missing)
    ratio = round(terms_covered / terms_total, 4) if terms_total else 0.0

    uncovered_papers = [
        paper.meta.id for paper in papers if paper.meta.id not in hit_paper_keys
    ]

    fingerprint = _manifest_value(manifest, "corpus_fingerprint", None)
    if not fingerprint:
        fingerprint = corpus_fingerprint(papers)

    return {
        "question": question,
        "generated_at": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope_id": scope_id,
        "manifest": {
            "version": _manifest_value(manifest, "version", 0),
            "corpus_fingerprint": fingerprint,
            "chunks": _manifest_value(manifest, "chunk_total", 0),
        },
        "plan": {
            "terms": terms,
            "expansions": list(plan.expansions),
            "variants": list(plan.variants),
        },
        "hits": packed_hits,
        "coverage": {
            "terms_total": terms_total,
            "terms_covered": terms_covered,
            "terms_missing": terms_missing,
            "ratio": ratio,
            "papers_hit": papers_hit,
            "papers_total": len(papers),
        },
        "gaps": {
            "uncovered_papers": uncovered_papers,
            "missing_terms": terms_missing,
            "note": _gap_note(uncovered_papers, terms_missing),
        },
        "memory": memory_state,
    }


def render_markdown(pack: Mapping[str, Any]) -> str:
    """把证据包渲染为 Markdown 报告。

    报告依次包含检索计划、逐条证据卡、覆盖度、缺口、研究记忆与
    末尾参考列表。

    Args:
        pack: :func:`build_pack` 生成的证据包。

    Returns:
        Markdown 文本。
    """
    question = _as_text(pack.get("question")) or "未命名问题"
    generated_at = _as_text(pack.get("generated_at"))
    scope_id = _as_text(pack.get("scope_id"))
    plan = _as_mapping(pack.get("plan"))
    hits = _as_sequence(pack.get("hits"))
    coverage = _as_mapping(pack.get("coverage"))
    gaps = _as_mapping(pack.get("gaps"))
    memory = _as_mapping(pack.get("memory"))

    lines: list[str] = [f"# {question}", ""]
    lines.append(f"- 生成时间：{generated_at or '未知'}")
    lines.append(f"- Scope：`{scope_id}`")
    lines.append("")

    lines.extend(["## 检索计划", ""])
    lines.append(f"- 术语：{_format_list(plan.get('terms'))}")
    lines.append(f"- 扩展：{_format_list(plan.get('expansions'))}")
    lines.append(f"- 变体：{_format_list(plan.get('variants'))}")
    lines.append("")

    lines.extend(["## 证据卡", ""])
    if not hits:
        lines.extend(["（本轮没有命中任何证据块）", ""])
    for index, raw_hit in enumerate(hits, start=1):
        hit = _as_mapping(raw_hit)
        paper = _as_mapping(hit.get("paper"))
        rank = hit.get("rank", index)
        title = _as_text(paper.get("title")) or _as_text(paper.get("id")) or "未知论文"
        section = _as_text(hit.get("section")) or "未标注小节"

        lines.append(f"### [P{rank}] {title}")
        lines.append("")
        lines.append(
            f"{title} · {_display(paper.get('year'))} · "
            f"{_display(paper.get('venue'))} · {section}"
        )
        lines.append("")
        lines.append(
            "- 命中理由："
            f"score={_format_score(hit.get('score'))}，"
            f"matched_by={_format_list(hit.get('matched_by'))}，"
            f"terms={_format_list(hit.get('terms'))}"
        )
        chunk_id = _as_text(hit.get("chunk_id"))
        if chunk_id:
            lines.append(f"- 证据块：`{chunk_id}`")
        lines.append("")
        lines.extend(_blockquote(_as_text(hit.get("snippet"))))
        lines.append("")

    plan_terms = [_as_text(term) for term in _as_sequence(plan.get("terms"))]
    missing_terms = [
        _as_text(term) for term in _as_sequence(coverage.get("terms_missing"))
    ]
    missing_keys = {term.casefold() for term in missing_terms}
    covered_terms = [
        term for term in plan_terms if term.casefold() not in missing_keys
    ]
    papers_hit = _as_sequence(coverage.get("papers_hit"))
    ratio = _as_float(coverage.get("ratio"))

    lines.extend(["## 覆盖度", ""])
    lines.append(
        f"- 覆盖率：{ratio * 100:.1f}%"
        f"（{coverage.get('terms_covered', 0)}/{coverage.get('terms_total', 0)} 个术语）"
    )
    lines.append(f"- 已覆盖术语：{_format_list(covered_terms)}")
    lines.append(f"- 缺失术语：{_format_list(missing_terms)}")
    lines.append(
        f"- 命中论文：{len(papers_hit)}/{coverage.get('papers_total', 0)}"
    )
    lines.append("")

    lines.extend(["## 缺口", ""])
    lines.append(
        f"- 未被命中的论文：{_format_list(gaps.get('uncovered_papers'))}"
    )
    lines.append(f"- 缺失术语：{_format_list(gaps.get('missing_terms'))}")
    note = _as_text(gaps.get("note"))
    if note:
        lines.append(f"- 小结：{note}")
    lines.append("")

    if _has_memory(memory):
        lines.extend(["## 研究记忆", ""])
        current_question = _as_text(memory.get("question"))
        if current_question:
            lines.append(f"- 当前问题：{current_question}")
        confirmed = _as_sequence(memory.get("confirmed"))
        if confirmed:
            lines.append(f"- 已确认：{_format_list(confirmed)}")
        excluded = _as_sequence(memory.get("excluded"))
        if excluded:
            lines.append("- 已排除：")
            for raw_item in excluded:
                item = _as_mapping(raw_item)
                text = _as_text(item.get("text")) or "（无说明）"
                paper_id = _as_text(item.get("paper"))
                suffix = f"（{paper_id}）" if paper_id else ""
                lines.append(f"  - {text}{suffix}")
        excluded_papers = _as_sequence(memory.get("excluded_papers"))
        if excluded_papers:
            lines.append(f"- 已排除论文：{_format_list(excluded_papers)}")
        reading = _as_mapping(memory.get("reading"))
        if reading:
            entries = [f"{paper_id}（{status}）" for paper_id, status in reading.items()]
            lines.append(f"- 阅读进度：{'、'.join(entries)}")
        lines.append("")

    lines.extend(["## 参考列表", ""])
    references: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_hit in hits:
        paper = _as_mapping(_as_mapping(raw_hit).get("paper"))
        paper_id = _as_text(paper.get("id"))
        key = paper_id or _as_text(paper.get("title"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        references.append(paper)

    if not references:
        lines.append("（无）")
    else:
        for index, paper in enumerate(references, start=1):
            title = _as_text(paper.get("title")) or _as_text(paper.get("id")) or "未知论文"
            authors = _as_text(paper.get("authors")) or "未知作者"
            venue = _as_text(paper.get("venue")) or "未知来源"
            year = _display(paper.get("year"))
            paper_id = _as_text(paper.get("id"))
            lines.append(
                f"{index}. {title}。{authors}。{venue}，{year}。`{paper_id}`"
            )

    return "\n".join(lines).rstrip() + "\n"


def save_pack(paths: Paths, pack: Mapping[str, Any]) -> Path:
    """把证据包写入 JSON 与 Markdown 文件并返回 JSON 路径。

    文件名使用 UTC 时间戳，形如 ``20261103T002530123Z``，写入
    ``paths.evidence`` 目录；目录不存在时会自动创建。

    Args:
        paths: PaperLens 运行期路径集合。
        pack: 待持久化的证据包。

    Returns:
        已写入的 JSON 文件路径。
    """
    paths.evidence.mkdir(parents=True, exist_ok=True)

    now = _utc_now()
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    json_path = paths.evidence / f"{stamp}.json"
    markdown_path = paths.evidence / f"{stamp}.md"

    json_path.write_text(
        json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(pack), encoding="utf-8")
    return json_path


def _utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def _unique_terms(terms: Iterable[str]) -> list[str]:
    """按大小写不敏感的方式去重术语，保留首次出现的写法。"""
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if not term or not term.strip():
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique


def _manifest_value(manifest: Any, key: str, default: Any) -> Any:
    """从清单对象或字典中读取字段，缺失时返回默认值。"""
    if manifest is None:
        return default
    if isinstance(manifest, Mapping):
        value = manifest.get(key, default)
    else:
        value = getattr(manifest, key, default)
    return default if value is None else value


def _paper_payload(meta: PaperMeta | None, paper_id: str) -> dict[str, Any]:
    """构造证据卡中的论文元数据。"""
    if meta is None:
        return {
            "id": paper_id,
            "title": paper_id,
            "authors": "",
            "year": 0,
            "venue": "",
        }
    return {
        "id": meta.id,
        "title": meta.title,
        "authors": meta.authors,
        "year": meta.year,
        "venue": meta.venue,
    }


def _body_excerpt(text: str, limit: int) -> str:
    """剥离来源行与小节标题后截取正文前 ``limit`` 个字符。"""
    lines = text.splitlines()

    body_start = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_SOURCE_PREFIX) and line.endswith("]]"):
            body_start = index + 1
        break

    start = body_start
    for index in range(body_start, len(lines)):
        if lines[index].lstrip().startswith(_HEADING_PREFIX):
            start = index + 1
            break

    body = "\n".join(lines[start:]).strip()
    return body[:limit]


def _gap_note(uncovered_papers: Sequence[str], missing_terms: Sequence[str]) -> str:
    """生成一句中文缺口总结。"""
    if not uncovered_papers and not missing_terms:
        return "本轮证据包已覆盖全部检索术语与论文，暂无缺口。"
    parts: list[str] = []
    if missing_terms:
        parts.append(f"{len(missing_terms)} 个术语未被任何证据覆盖")
    if uncovered_papers:
        parts.append(f"{len(uncovered_papers)} 篇论文未被命中")
    return "，".join(parts) + "，建议补充检索或调整问题表述。"


def _as_text(value: Any) -> str:
    """把任意值规范化为字符串。"""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """把任意值规范化为只读映射。"""
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    """把任意值规范化为序列，字符串与字节串不作为序列处理。"""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _as_float(value: Any) -> float:
    """把任意值规范化为浮点数，失败时返回 ``0.0``。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _display(value: Any) -> str:
    """返回适合展示的字段值。"""
    if value is None or value == "" or value == 0:
        return "未知"
    return str(value)


def _format_score(value: Any) -> str:
    """把分数格式化为四位小数。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.4f}"
    return "未知"


def _format_list(values: Any) -> str:
    """把列表格式化为中文顿号分隔的文本。"""
    if isinstance(values, str):
        values = [values]
    if not values:
        return "无"
    try:
        items = [str(value) for value in values]
    except TypeError:
        return str(values)
    return "、".join(items) if items else "无"


def _blockquote(text: str) -> list[str]:
    """把文本渲染为 Markdown 引用块。"""
    body = text.strip()
    if not body:
        return ["> （无摘要）"]
    return [f"> {line}" if line.strip() else ">" for line in body.splitlines()]


def _has_memory(memory: Mapping[str, Any]) -> bool:
    """判断研究记忆是否包含可展示的内容。"""
    return bool(
        memory.get("question")
        or memory.get("confirmed")
        or memory.get("excluded")
        or memory.get("excluded_papers")
        or memory.get("reading")
    )
