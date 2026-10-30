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
_POOL_PER_QUERY = 60
_TOKEN = re.compile(r"[a-z]+|\d+")
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
    """Build a few complementary lexical probes for one question."""

    variants = [question.strip()]
    if numbers:
        variants.append(" ".join(numbers))
    if terms:
        variants.append(" ".join(numbers) + " " + " ".join(terms[:5]) if numbers else " ".join(terms[:5]))
    unique: list[str] = []
    for variant in variants:
        cleaned = " ".join(variant.split())
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return tuple(unique)


def _score(text: str, terms: tuple[str, ...], numbers: tuple[str, ...]) -> float:
    """Rank one candidate page by evidence-term and numeric coverage."""

    present = set(_tokens(text))
    score = 0.0
    if terms:
        score += 3.0 * sum(1 for term in terms if term in present) / len(terms)
    if numbers:
        score += 2.0 * sum(1 for number in numbers if number in present) / len(numbers)
    return score


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct physical-page citations for one question."""

    from powercontext.artifacts.models import MemoryCitation
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if limit <= 0 or not isinstance(question, str) or not question.strip():
        return ()

    terms, numbers = _question_profile(question)
    candidates: list[tuple[int, Any]] = []
    seen: set[str] = set()
    for variant in _query_variants(question, terms, numbers):
        page = await memory.search(
            SearchMemoryRequest(query=variant, mode="fts", limit=_POOL_PER_QUERY)
        )
        for rank, hit in enumerate(page.hits):
            if hit.entry_version_id in seen:
                continue
            seen.add(hit.entry_version_id)
            candidates.append((rank, hit))

    ranked = sorted(
        candidates,
        key=lambda item: (_score(item[1].text, terms, numbers), -item[0]),
        reverse=True,
    )

    results: list[MemoryCitation] = []
    used_pages: set[str] = set()
    for _, hit in ranked:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
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
