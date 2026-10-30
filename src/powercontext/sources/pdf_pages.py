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

"""Parse a complete PDF into physical-page Sources and retrieve evidence pages."""

from __future__ import annotations

import hashlib
import io
import math
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.artifacts.models import MemoryCitation

PDF_PAGE_SOURCE_NAME = "pdf-page"
_POOL_PER_QUERY = 80
_MAX_RESOLVE = 120
_MAX_DOCUMENTS = 32
_TOKEN = re.compile(r"[a-z]+|\d+")
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
_SPACES = re.compile(r"\s+")
_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "what",
        "which",
        "who",
        "how",
        "why",
        "when",
        "where",
        "did",
        "does",
        "do",
        "has",
        "have",
        "had",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
        "company",
        "companies",
        "please",
    }
)


class PDFDocumentInput(BaseModel):
    """One complete original PDF payload."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """The adapter-native value of one physical PDF page."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """One captured physical page kept with its document identity."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable, bounded identity of one physical PDF page."""

    identity = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    bounded = hashlib.sha256(f"{document_id}\x00{document_sha256}\x00{page_number}".encode()).hexdigest()
    return f"pdf:{bounded}"


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one original PDF, or fail without partial pages."""

    document_id = value.document_id
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty, non-blank string")  # noqa: TRY003
    content = value.content
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("PDF content must be non-empty bytes")  # noqa: TRY003

    content = bytes(content)
    document_sha256 = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a user password")  # noqa: TRY003
        pages = tuple(
            PDFPageInput(
                document_id=document_id,
                document_sha256=document_sha256,
                page_number=index,
                text=(page.extract_text() or "").strip(),
            )
            for index, page in enumerate(reader.pages, start=1)
        )
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001
        raise ValueError(f"PDF could not be parsed completely: {error}") from error  # noqa: TRY003

    if not pages:
        raise ValueError("PDF contains no physical pages")  # noqa: TRY003
    return pages


class PDFPageSourceAdapter:
    """Resolve and read one captured physical page."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one physical page as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


def _tokens(value: str) -> list[str]:
    return _TOKEN.findall(value.casefold())


def _numbers(value: str) -> set[str]:
    """Extract comparable numeric evidence, joining values split by thousands separators."""

    return {match.replace(",", "") for match in _NUMBER.findall(value)}


def _normalized_text(value: str) -> str:
    return _SPACES.sub(" ", value.casefold()).strip()


def _document_of(text: str) -> str:
    """Read the document identity that every page projection prefixes to its text."""

    head, _, _body = text.partition("\n")
    return head.strip()


def _page_body(text: str) -> str:
    """Drop the leading document identity so it cannot supply page evidence."""

    _head, separator, body = text.partition("\n")
    return body if separator else text


def _question_profile(question: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a natural-language question into content terms and numeric evidence."""

    tokens = _tokens(question)
    numbers = tuple(dict.fromkeys(token for token in tokens if token.isdigit()))
    terms: list[str] = []
    for token in tokens:
        if token.isdigit() or token in _FUNCTION_WORDS or len(token) < 3:
            continue
        if token not in terms:
            terms.append(token)
    return tuple(terms), numbers


def _query_variants(question: str, terms: tuple[str, ...], numbers: tuple[str, ...]) -> tuple[str, ...]:
    """Build complementary lexical probes so one weak probe cannot hide a page."""

    variants = [question.strip()]
    if numbers:
        variants.append(" ".join(numbers))
    if terms:
        lead = " ".join(terms[:5])
        variants.append(f"{' '.join(numbers)} {lead}".strip() if numbers else lead)
    if len(terms) > 5:
        variants.append(" ".join(terms[5:10]))
    unique: list[str] = []
    for variant in variants:
        cleaned = " ".join(variant.split())
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return tuple(unique)


def _names_document(document_id: str, question_tokens: frozenset[str]) -> bool:
    """Decide whether the question names the document it is asking about.

    Only alphabetic identity tokens count: a shared fiscal year is not a document name.
    """

    return bool(
        {
            token
            for token in _tokens(document_id)
            if len(token) >= 4 and not token.isdigit()
        }
        & question_tokens
    )


def _restrict_to_named_documents(
    items: list[tuple[int, Any]],
    question_tokens: frozenset[str],
) -> list[tuple[int, Any]]:
    """Prefer pages of the filing a question names when one scope mixes filings.

    The restriction is only applied when the candidate pool looks like a small set of
    documents and the question singles out a strict subset of them; otherwise the pool is
    returned untouched so an unusual projection shape can never empty the result.
    """

    documents = {_document_of(hit.text) for _rank, hit in items}
    if not 2 <= len(documents) <= _MAX_DOCUMENTS:
        return items
    mentioned = {document for document in documents if _names_document(document, question_tokens)}
    if not mentioned or len(mentioned) == len(documents):
        return items
    restricted = [item for item in items if _document_of(item[1].text) in mentioned]
    return restricted or items


def _score(
    hit: Any,
    terms: tuple[str, ...],
    numbers: tuple[str, ...],
    weights: dict[str, float],
    total_weight: float,
    phrase: str,
    question_tokens: frozenset[str],
) -> float:
    """Rank one candidate page by weighted term coverage, numbers, and document match."""

    body = _page_body(hit.text)
    present = set(_tokens(body))
    matched = sum(weights.get(term, 0.0) for term in terms if term in present)
    score = 3.0 * (matched / total_weight if total_weight else 0.0)
    if numbers:
        score += 2.5 * len(set(numbers) & _numbers(body)) / len(set(numbers))
    if _names_document(_document_of(hit.text), question_tokens):
        # A scope may mix several filings: the named document must outrank the rest.
        score += 6.0
    if len(phrase) >= 24 and phrase in _normalized_text(body):
        score += 1.5
    return score


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct physical-page citations for one question."""

    from powercontext.artifacts.models import MemoryCitation
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if limit <= 0 or not isinstance(question, str) or not question.strip():
        return ()

    terms, numbers = _question_profile(question)
    question_tokens = frozenset(_tokens(question))
    phrase = _normalized_text(question)

    collected: dict[str, tuple[int, Any]] = {}
    for variant in _query_variants(question, terms, numbers):
        page = await memory.search(
            SearchMemoryRequest(query=variant, mode="fts", limit=_POOL_PER_QUERY)
        )
        for rank, hit in enumerate(page.hits):
            prior = collected.get(hit.entry_version_id)
            if prior is None or rank < prior[0]:
                collected[hit.entry_version_id] = (rank, hit)

    if not collected:
        for probe in (*numbers[:2], *terms[:3]):
            page = await memory.search(
                SearchMemoryRequest(query=probe, mode="fts", limit=_POOL_PER_QUERY)
            )
            for rank, hit in enumerate(page.hits):
                collected.setdefault(hit.entry_version_id, (rank, hit))

    if not collected:
        return ()

    pool = list(collected.values())
    preferred = _restrict_to_named_documents(pool, question_tokens)
    if preferred is pool:
        items = pool
    else:
        # Keep the named filing first, then offer the rest as recall backfill.
        preferred_ids = {hit.entry_version_id for _rank, hit in preferred}
        items = preferred + [
            item for item in pool if item[1].entry_version_id not in preferred_ids
        ]
    if not items:
        return ()

    total = len(items)
    document_frequency = dict.fromkeys(terms, 0)
    for _rank, hit in items:
        present = set(_tokens(_page_body(hit.text)))
        for term in terms:
            if term in present:
                document_frequency[term] += 1
    weights = {
        term: math.log(1.0 + total / (1.0 + count)) for term, count in document_frequency.items()
    }
    total_weight = sum(weights.values()) or 1.0

    ranked = sorted(
        items,
        key=lambda item: (
            _score(item[1], terms, numbers, weights, total_weight, phrase, question_tokens),
            -item[0],
        ),
        reverse=True,
    )

    results: list[MemoryCitation] = []
    used_pages: set[str] = set()
    for _rank, hit in ranked[:_MAX_RESOLVE]:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        except Exception:  # noqa: BLE001 - a stale candidate must not fail the whole search
            continue
        if len(entry.sources) != 1:
            # Only an unambiguous single-page entry can back one exact page citation.
            continue
        source_id = entry.sources[0].source_id
        if source_id in used_pages:
            continue
        used_pages.add(source_id)
        results.append(citation)
        if len(results) >= limit:
            break
    return tuple(results)
