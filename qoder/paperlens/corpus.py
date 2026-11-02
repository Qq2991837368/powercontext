"""PaperLens 语料读取、切块与指纹计算。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MIN_CHUNK_CHARS = 160
MAX_CHUNK_CHARS = 260
_REQUIRED_FIELDS = ("id", "title", "authors", "year", "venue", "tags")
_HEADING_PREFIX = re.compile(r"^\d+\.\s*")


@dataclass(frozen=True)
class PaperMeta:
    """论文的元数据。"""

    id: str
    title: str
    authors: str
    year: int
    venue: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Chunk:
    """论文切分后的文本块。"""

    chunk_id: str
    paper_id: str
    section: str
    ordinal: int
    text: str
    sha256: str
    char_count: int


@dataclass(frozen=True)
class Paper:
    """一篇已解析的论文。"""

    meta: PaperMeta
    path: str
    sha256: str
    chunks: tuple[Chunk, ...]


def _strip_quotes(value: str) -> str:
    """去掉值两端成对的单引号或双引号。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_tags(raw: str) -> tuple[str, ...]:
    """解析 `[a, b]` 形式的标签列表。"""
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return tuple(
        _strip_quotes(part.strip())
        for part in value.split(",")
        if part.strip()
    )


def _split_front_matter(raw_text: str, *, source: str) -> tuple[dict[str, str], list[str]]:
    """拆分 front matter 与正文。"""
    lines = raw_text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source}: 缺少 front matter 起始分隔符 '---'")
    closing = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing == -1:
        raise ValueError(f"{source}: front matter 缺少结束分隔符 '---'")
    fields: dict[str, str] = {}
    for offset in range(1, closing):
        line = lines[offset].strip()
        if not line:
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not key:
            raise ValueError(f"{source}: front matter 第 {offset + 1} 行不是 'key: value' 形式")
        fields[key] = value.strip()
    return fields, lines[closing + 1:]


def _build_meta(fields: dict[str, str], *, source: str) -> PaperMeta:
    """校验并构造论文元数据。"""
    missing = [name for name in _REQUIRED_FIELDS if not fields.get(name, "").strip()]
    if missing:
        raise ValueError(f"{source}: front matter 缺少必需字段: {', '.join(missing)}")
    raw_year = fields["year"].strip()
    try:
        year = int(raw_year)
    except ValueError as exc:
        raise ValueError(f"{source}: front matter 中 year 不是整数: {raw_year!r}") from exc
    return PaperMeta(
        id=_strip_quotes(fields["id"].strip()),
        title=_strip_quotes(fields["title"].strip()),
        authors=_strip_quotes(fields["authors"].strip()),
        year=year,
        venue=_strip_quotes(fields["venue"].strip()),
        tags=_parse_tags(fields["tags"]),
    )


def _section_slug(section: str) -> str:
    """由小节标题生成 chunk id 用的 slug。"""
    slug = re.sub(r"\s+", "-", section[:12])
    return slug or "section"


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """按行首 `## ` 切分小节。"""
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_title is not None:
                sections.append((current_title, current_lines))
            raw_title = line[3:].strip()
            title = _HEADING_PREFIX.sub("", raw_title).strip()
            current_title = title or "Untitled"
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections.append((current_title, current_lines))
    return sections


def _split_paragraphs(lines: list[str]) -> list[str]:
    """按空行切分小节内的段落。"""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _pack_paragraphs(paragraphs: list[str]) -> list[tuple[int, str]]:
    """把段落装箱成 160–260 字符的正文块，并返回首段序号。"""
    packed: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_len = 0
    buffer_start = 0

    def flush() -> None:
        """输出当前缓冲的正文块。"""
        nonlocal buffer, buffer_len, buffer_start
        if buffer:
            packed.append((buffer_start, "\n\n".join(buffer)))
            buffer = []
            buffer_len = 0
            buffer_start = 0

    for index, paragraph in enumerate(paragraphs, start=1):
        paragraph_len = len(paragraph)
        if paragraph_len > MAX_CHUNK_CHARS:
            flush()
            packed.append((index, paragraph))
            continue
        if not buffer:
            buffer = [paragraph]
            buffer_len = paragraph_len
            buffer_start = index
            continue
        if buffer_len + 2 + paragraph_len <= MAX_CHUNK_CHARS:
            buffer.append(paragraph)
            buffer_len += 2 + paragraph_len
        else:
            flush()
            buffer = [paragraph]
            buffer_len = paragraph_len
            buffer_start = index
    flush()
    return packed


def _build_chunks(
    paper_id: str,
    sections: list[tuple[str, list[str]]],
    *,
    source: str,
) -> tuple[Chunk, ...]:
    """把论文小节转换为 chunk 元组。"""
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for section, lines in sections:
        slug = _section_slug(section)
        for ordinal, body in _pack_paragraphs(_split_paragraphs(lines)):
            text = f"## {section}\n{body}"
            chunk_id = f"{paper_id}#{slug}#{ordinal}"
            if chunk_id in seen:
                raise ValueError(f"{source}: 生成的 chunk id 重复: {chunk_id}")
            seen.add(chunk_id)
            chunks.append(Chunk(
                chunk_id=chunk_id,
                paper_id=paper_id,
                section=section,
                ordinal=ordinal,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                char_count=len(text),
            ))
    return tuple(chunks)


def _load_paper(path: Path) -> Paper:
    """读取并解析单篇论文。"""
    raw = path.read_bytes()
    source = str(path)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: 不是有效的 UTF-8 文本") from exc
    fields, body_lines = _split_front_matter(text, source=source)
    meta = _build_meta(fields, source=source)
    chunks = _build_chunks(meta.id, _split_sections(body_lines), source=source)
    return Paper(
        meta=meta,
        path=source,
        sha256=hashlib.sha256(raw).hexdigest(),
        chunks=chunks,
    )


def load_papers(papers_dir: Path) -> tuple[Paper, ...]:
    """读取目录下全部 Markdown 论文。"""
    if not papers_dir.is_dir():
        raise ValueError(f"语料目录不存在: {papers_dir}")
    papers: list[Paper] = []
    seen: dict[str, Path] = {}
    for path in sorted(papers_dir.glob("*.md")):
        paper = _load_paper(path)
        previous = seen.get(paper.meta.id)
        if previous is not None:
            raise ValueError(f"{path}: 论文 id {paper.meta.id!r} 与 {previous} 重复")
        seen[paper.meta.id] = path
        papers.append(paper)
    return tuple(papers)


def corpus_fingerprint(papers: tuple[Paper, ...]) -> str:
    """计算语料整体指纹。"""
    ordered = sorted(papers, key=lambda paper: paper.meta.id)
    payload = "\n".join(f"{paper.path}:{paper.sha256}" for paper in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
