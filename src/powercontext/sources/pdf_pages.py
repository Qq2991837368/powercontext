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
from dataclasses import dataclass, replace
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
# Document identities carry brand tokens such as "3m" that _TOKEN would split apart.
_IDENTITY = re.compile(r"[a-z0-9]+")
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

# Question scaffolding that carries no retrieval signal on a filing page.
_NOISE_TERMS = frozenset(
    {
        "any",
        "answer",
        "approximately",
        "assume",
        "assuming",
        "based",
        "calculate",
        "compute",
        "computed",
        "data",
        "define",
        "defined",
        "ensure",
        "explicitly",
        "given",
        "important",
        "major",
        "materially",
        "metric",
        "metrics",
        "ongoing",
        "outlined",
        "percent",
        "percentage",
        "percents",
        "perspective",
        "primarily",
        "quantity",
        "report",
        "reported",
        "reports",
        "respond",
        "response",
        "sell",
        "sells",
        "state",
        "stated",
        "sum",
        "units",
        "use",
        "used",
        "useful",
        "uses",
        "using",
        # Question scaffolding that never appears on the filing page it asks about.
        "about",
        "according",
        "address",
        "also",
        "asked",
        "between",
        "can",
        "clearly",
        "compare",
        "compared",
        "comparison",
        "could",
        "decimal",
        "describe",
        "described",
        "detail",
        "details",
        "each",
        "exactly",
        "explain",
        "explanation",
        "follow",
        "following",
        "identify",
        "if",
        "information",
        "item",
        "like",
        "line",
        "list",
        "listed",
        "mention",
        "mentioned",
        "more",
        "not",
        "one",
        "only",
        "place",
        "precisely",
        "question",
        "represent",
        "round",
        "rounding",
        "should",
        "show",
        "shown",
        "specifically",
        "such",
        "tell",
        "than",
        "then",
        "there",
        "two",
        "usd",
        "utilize",
        "utilized",
        "utilizing",
        "within",
        "would",
        "your",
    }
)

_STOP_TERMS = _FUNCTION_WORDS | _NOISE_TERMS

_BM25_K1 = 1.2
_BM25_B = 0.75

# Structural signals break ties that term statistics alone cannot separate.
_NAMED_BONUS = 6.0
_STATEMENT_BONUS = 2.0
_STATEMENT_TYPE_BONUS = 4.0
_RESULTS_BONUS = 2.0
_BUSINESS_BONUS = 3.0
_BIGRAM_BONUS = 2.0
_PHRASE_BONUS = 1.5
_DENSITY_BONUS = 0.5
_DROP_IDENTITY_TERMS = True

# Page-side wording that answers a question concept without reusing its word.
# Keys and values are stems (see _stem) so plural line items still match.
_EXPANSIONS: dict[str, frozenset[str]] = {
    "geographie": frozenset({"geographic", "geography", "region", "market", "territor", "countr"}),
    "geography": frozenset({"geographic", "geography", "region", "market", "territor", "countr"}),
    "battle": frozenset({"proceeding", "litigation", "lawsuit", "contingenc", "dispute"}),
    "legal": frozenset({"legal", "proceeding", "litigation", "lawsuit", "contingenc"}),
    "product": frozenset({"product", "service", "offering", "solution", "portfolio"}),
    "service": frozenset({"service", "product", "offering", "solution"}),
    "cyclicality": frozenset({"cyclical", "seasonal", "cycle", "economic"}),
    "cyclical": frozenset({"cyclical", "seasonal", "cycle"}),
    "production": frozenset({"production", "manufactur", "rate", "capacity"}),
    "customer": frozenset({"customer", "client", "purchaser", "concentration"}),
    "concentration": frozenset({"concentration", "customer", "client", "purchaser"}),
    "liquidity": frozenset({"liquidity", "liquid", "ratio", "current", "cash"}),
    "quick": frozenset({"quick", "ratio", "liquid", "current"}),
    "payable": frozenset({"payable", "outstanding", "creditor"}),
    "receivable": frozenset({"receivable", "collection", "outstanding"}),
    "segment": frozenset({"segment", "reporting", "division"}),
    "tax": frozenset({"tax", "effective", "rate"}),
    "margin": frozenset({"margin", "profit", "profitability", "income"}),
    "profit": frozenset({"profit", "margin", "income"}),
    "income": frozenset({"income", "operation", "operating", "earning", "profit", "loss", "sale", "revenue"}),
    "revenue": frozenset({"revenue", "sale", "turnover", "income"}),
    "change": frozenset(
        {
            "change",
            "changed",
            "changing",
            "increase",
            "increased",
            "increasing",
            "decrease",
            "decreased",
            "decline",
            "declined",
            "growth",
            "grew",
            "variance",
        }
    ),
    "capex": frozenset({"capex", "expenditure", "capital", "ppne", "property", "equipment"}),
    "capital": frozenset({"capital", "capex", "expenditure", "property", "equipment"}),
    "debt": frozenset({"debt", "borrow", "obligation", "note"}),
    "lease": frozenset({"lease", "obligation"}),
    "dividend": frozenset({"dividend", "share"}),
    "ebitda": frozenset({"ebitda", "operatingincome", "depreciation", "amortization"}),
    "restructuring": frozenset({"restructuring", "restructur", "severance", "charge"}),
    "growth": frozenset({"growth", "increase", "decline", "decrease"}),
    "inventory": frozenset({"inventory", "inventorie"}),
    "drive": frozenset({"increase", "decrease", "due", "reflect", "contribut", "primarily"}),
}


def _expanded(term: str) -> frozenset[str]:
    """Return every page-side stem that answers one question term."""

    return _EXPANSIONS.get(term, frozenset({term}))


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


def _stem(token: str) -> str:
    """Fold plural forms so question terms meet statement line items exactly."""

    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _is_fiscal_year(token: str) -> bool:
    """Every page of one filing repeats its fiscal years, so a year is not evidence."""

    return len(token) == 4 and token[:2] in {"19", "20"}


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


@dataclass(frozen=True, slots=True)
class _Question:
    """Precomputed lexical view of one natural-language question."""

    text: str
    terms: tuple[str, ...]
    numbers: tuple[str, ...]
    tokens: frozenset[str]
    bigrams: frozenset[tuple[str, str]]
    phrase: str
    wants_statement: bool
    quantitative: bool
    statement_types: frozenset[str]
    driver: bool
    business_overview: bool


_QUANTITATIVE_HINTS = frozenset(
    {
        "amount",
        "basis",
        "calculate",
        "capex",
        "capital",
        "cash",
        "change",
        "cost",
        "costs",
        "debt",
        "decline",
        "decrease",
        "depreciation",
        "dividend",
        "ebitda",
        "effective",
        "expenditure",
        "expenditures",
        "expense",
        "expenses",
        "growth",
        "impairment",
        "income",
        "increase",
        "inventory",
        "lease",
        "loss",
        "margin",
        "payable",
        "percent",
        "percentage",
        "profit",
        "quantity",
        "rate",
        "ratio",
        "receivable",
        "revenue",
        "revenues",
        "sale",
        "sales",
        "spend",
        "spending",
        "tax",
        "total",
        "value",
    }
)

# Terms that appear on nearly every filing page; probing them alone returns noise.
_GENERIC_TERMS = frozenset(
    {
        "cash",
        "company",
        "cost",
        "costs",
        "fiscal",
        "flow",
        "flows",
        "income",
        "percent",
        "profit",
        "revenue",
        "revenues",
        "statement",
        "statements",
        "total",
        "value",
        "year",
        "years",
    }
)


def _distinctive(terms: tuple[str, ...], limit: int = 4) -> tuple[str, ...]:
    """Pick rare, question-defining words worth a dedicated single-term probe."""

    picked = [term for term in terms if len(term) >= 4 and term not in _GENERIC_TERMS]
    return tuple(picked[:limit])


def _windows(terms: tuple[str, ...], size: int = 3, limit: int = 6) -> tuple[str, ...]:
    """Cover a long question with short focused probes instead of one weak union."""

    if len(terms) <= size:
        return (" ".join(terms),) if terms else ()
    span = min(len(terms) - size + 1, limit)
    return tuple(" ".join(terms[index : index + size]) for index in range(span))


# Question wording that points at one primary financial statement by name.
_INCOME_MARKERS = (
    "consolidated statement of income",
    "consolidated statements of income",
    "consolidated statement of operations",
    "consolidated statements of operations",
)

_STATEMENT_TYPE_MARKERS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (
        frozenset({"cash", "flow"}),
        ("consolidated statement of cash flows", "consolidated statements of cash flows"),
    ),
    (
        frozenset({"balance"}),
        ("consolidated balance sheet",),
    ),
    (
        frozenset({"comprehensive"}),
        (
            "consolidated statement of comprehensive income",
            "consolidated statements of comprehensive income",
        ),
    ),
    (frozenset({"income", "statement"}), _INCOME_MARKERS),
    # "P&L statement" tokenizes down to its two initial letters.
    (frozenset({"p", "l"}), _INCOME_MARKERS),
)

_DRIVER_WORDS = frozenset({"drove", "drive", "driven", "driver", "drives"})

# Questions that ask for a description of the company instead of a computed figure.
_BUSINESS_TOPICS = frozenset({"geographie", "geography", "customer", "client"})


def _statement_types(stemmed: set[str]) -> frozenset[str]:
    """Read every primary statement a question points at by name."""

    return frozenset(
        marker
        for required, markers in _STATEMENT_TYPE_MARKERS
        if required <= stemmed
        for marker in markers
    )


def _prepare(question: str) -> _Question:
    """Split a question into content terms, numeric evidence, and phrase signals."""

    tokens = _tokens(question)
    numbers = tuple(
        dict.fromkeys(
            token for token in tokens if token.isdigit() and not _is_fiscal_year(token)
        )
    )
    terms: list[str] = []
    for token in tokens:
        if token.isdigit() or token in _STOP_TERMS or len(token) < 3:
            continue
        stemmed = _stem(token)
        if stemmed not in terms:
            terms.append(stemmed)
    bigrams = frozenset(
        (_stem(left), _stem(right))
        for left, right in zip(tokens, tokens[1:])
        if len(left) >= 3
        and len(right) >= 3
        and left not in _STOP_TERMS
        and right not in _STOP_TERMS
    )
    stems = {_stem(token) for token in tokens}
    quantitative = bool(set(tokens) & _QUANTITATIVE_HINTS)
    return _Question(
        text=question.strip(),
        terms=tuple(terms),
        numbers=numbers,
        tokens=frozenset(tokens) | _identity_tokens(question),
        bigrams=bigrams,
        phrase=_normalized_text(question),
        wants_statement=bool({"statement", "statements"} & set(tokens)),
        quantitative=quantitative,
        statement_types=_statement_types(stems),
        driver=bool(_DRIVER_WORDS & set(tokens)),
        business_overview=(
            not quantitative
            and bool(_BUSINESS_TOPICS & stems or {"product", "service"} <= stems)
        ),
    )


def _query_variants(question: _Question) -> tuple[str, ...]:
    """Build complementary lexical probes so one weak probe cannot hide a page."""

    variants = [question.text]
    if question.numbers:
        variants.append(" ".join(question.numbers))
    if question.terms:
        variants.append(" ".join(question.terms[:5]))
        variants.extend(_windows(question.terms))
        if question.numbers:
            variants.append(f"{' '.join(question.numbers)} {' '.join(question.terms[:3])}")
        # A rare word alone reaches pages that the multi-term admission floor rejects.
        variants.extend(_distinctive(question.terms))
    unique: list[str] = []
    for variant in variants:
        cleaned = " ".join(variant.split())
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return tuple(unique)


def _identity_tokens(value: str) -> frozenset[str]:
    """Read brand-level identity tokens, keeping short names such as "3m" and "amd"."""

    return frozenset(
        token
        for token in _IDENTITY.findall(value.casefold())
        if len(token) >= 2 and not token.isdigit()
    )


def _names_document(document_id: str, question_tokens: frozenset[str]) -> bool:
    """Decide whether the question names the document it is asking about.

    Only alphabetic identity tokens count: a shared fiscal year is not a document name.
    """

    return bool(_identity_tokens(document_id) & question_tokens)


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


_STATEMENT_MARKERS = (
    "consolidated statement of income",
    "consolidated statements of income",
    "consolidated statement of operations",
    "consolidated statements of operations",
    "consolidated statement of cash flows",
    "consolidated statements of cash flows",
    "consolidated balance sheet",
    "consolidated statements of financial position",
    "consolidated statement of financial position",
    "consolidated statement of comprehensive income",
    "consolidated statements of comprehensive income",
    "consolidated statements of changes in equity",
)


def _stem_counts(tokens: list[str]) -> dict[str, int]:
    """Count stemmed page tokens once so per-term frequencies stay cheap."""

    counts: dict[str, int] = {}
    for token in tokens:
        stem = _stem(token)
        counts[stem] = counts.get(stem, 0) + 1
    return counts


def _bm25(
    question: _Question,
    counts: dict[str, int],
    weights: dict[str, float],
    length: int,
    average_length: float,
) -> float:
    """Sum saturation- and length-normalized term frequencies for one page."""

    average = average_length if average_length > 0.0 else float(length)
    length_norm = 1.0 - _BM25_B + _BM25_B * (length / average)
    score = 0.0
    for term in question.terms:
        frequency = sum(counts.get(stem, 0) for stem in _expanded(term))
        if frequency:
            score += (
                weights.get(term, 0.0)
                * frequency
                * (_BM25_K1 + 1.0)
                / (frequency + _BM25_K1 * length_norm)
            )
    return score


def _score(
    question: _Question,
    hit: Any,
    weights: dict[str, float],
    stem_counts: dict[str, int] | None = None,
    body_tokens: list[str] | None = None,
    average_length: float = 0.0,
) -> float:
    """Rank one candidate page by BM25 terms, phrases, numbers and structure."""

    body = _page_body(hit.text)
    body_tokens = _tokens(body) if body_tokens is None else body_tokens
    if stem_counts is None:
        stem_counts = _stem_counts(body_tokens)
    score = _bm25(question, stem_counts, weights, len(body_tokens) or 1, average_length)

    if question.bigrams:
        pairs = {(_stem(left), _stem(right)) for left, right in zip(body_tokens, body_tokens[1:])}
        score += _BIGRAM_BONUS * len(question.bigrams & pairs) / len(question.bigrams)

    if _names_document(_document_of(hit.text), question.tokens):
        # A scope may mix several filings: the named document must outrank the rest.
        score += _NAMED_BONUS

    normalized = _normalized_text(body)
    if len(question.phrase) >= 24 and question.phrase in normalized:
        score += _PHRASE_BONUS

    if (question.quantitative or question.wants_statement) and any(
        marker in normalized for marker in _STATEMENT_MARKERS
    ):
        # Primary-statement pages carry metrics without repeating the question's wording.
        score += _STATEMENT_BONUS

    head = normalized[:300]
    if question.statement_types and any(marker in head for marker in question.statement_types):
        # The question named a statement: its title page is the page being quoted.
        score += _STATEMENT_TYPE_BONUS

    if question.driver and "results of operations" in head:
        # "What drove the change" is answered by the MD&A results discussion.
        score += _RESULTS_BONUS

    if question.business_overview and "item 1. business" in normalized:
        # A describe-the-company question is answered by the business section itself.
        score += _BUSINESS_BONUS

    if question.quantitative and body_tokens:
        density = sum(1 for token in body_tokens if token.isdigit()) / len(body_tokens)
        score += min(1.0, density / 0.25) * _DENSITY_BONUS

    return score


@dataclass(frozen=True, slots=True)
class _Page:
    """One candidate page together with the citation that resolves it."""

    text: str
    citation: MemoryCitation


async def _listed_pages(memory: Any) -> list[_Page]:
    """Enumerate every page of the scope so no admission floor can hide evidence."""

    lister = getattr(memory, "list", None)
    if not callable(lister):
        return []
    listed = await lister()
    pages: list[_Page] = []
    for record in getattr(listed, "entries", ()):
        entry = record.entry
        if len(entry.sources) != 1:
            # Only an unambiguous single-page entry can back one exact page citation.
            continue
        if not entry.sources[0].source_id.startswith("pdf:"):
            continue
        pages.append(_Page(text=entry.text, citation=record.citation))
    return pages


async def _searched_pages(memory: Any, profile: _Question) -> list[_Page]:
    """Fall back to the generic search pool when the scope cannot be enumerated."""

    from powercontext.artifacts.models import MemoryCitation
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    collected: dict[str, tuple[int, Any]] = {}
    for variant in _query_variants(profile):
        page = await memory.search(
            SearchMemoryRequest(query=variant, mode="auto", limit=_POOL_PER_QUERY)
        )
        for rank, hit in enumerate(page.hits):
            prior = collected.get(hit.entry_version_id)
            if prior is None or rank < prior[0]:
                collected[hit.entry_version_id] = (rank, hit)

    pages: list[_Page] = []
    for _rank, hit in list(collected.values())[:_MAX_RESOLVE]:
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
            continue
        pages.append(_Page(text=entry.text, citation=citation))
    return pages


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct physical-page citations for one question."""

    if limit <= 0 or not isinstance(question, str) or not question.strip():
        return ()

    profile = _prepare(question)
    pages = await _listed_pages(memory)
    if not pages:
        pages = await _searched_pages(memory, profile)
    if not pages:
        return ()

    documents = {_document_of(page.text) for page in pages}
    # A filing's own name is written on most of its pages: matching it says nothing.
    identity = (
        {token for document in documents for token in _identity_tokens(document)}
        if _DROP_IDENTITY_TERMS
        else set()
    )
    kept = tuple(term for term in profile.terms if term not in identity)
    if kept and len(kept) != len(profile.terms):
        profile = replace(
            profile,
            terms=kept,
            bigrams=frozenset(
                pair
                for pair in profile.bigrams
                if pair[0] not in identity and pair[1] not in identity
            ),
        )
    mentioned = {document for document in documents if _names_document(document, profile.tokens)}
    restrict = (
        2 <= len(documents) <= _MAX_DOCUMENTS and bool(mentioned) and len(mentioned) < len(documents)
    )

    analysed = [(page, _tokens(_page_body(page.text))) for page in pages]
    prepared = [(page, tokens, _stem_counts(tokens)) for page, tokens in analysed]
    if restrict:
        # Keep the named filing first, then offer the rest as recall backfill.
        named = [item for item in prepared if _document_of(item[0].text) in mentioned]
        other = [item for item in prepared if _document_of(item[0].text) not in mentioned]
    else:
        named, other = prepared, []

    # Rarity and page length must be read inside the filing the question targets:
    # a word that is common across five filings can still be rare in one of them.
    corpus = named or prepared
    total = len(corpus)
    average_length = sum(len(tokens) for _page, tokens, _counts in corpus) / total or 1.0
    document_frequency = dict.fromkeys(profile.terms, 0)
    for _page, _tokens_list, counts in corpus:
        for term in profile.terms:
            if any(counts.get(stem, 0) for stem in _expanded(term)):
                document_frequency[term] += 1
    weights = {
        term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
        for term, count in document_frequency.items()
    }

    def _ordered(items: list[tuple[_Page, list[str], dict[str, int]]]) -> list[_Page]:
        ranked = sorted(
            enumerate(items),
            key=lambda item: (
                _score(
                    profile,
                    item[1][0],
                    weights,
                    item[1][2],
                    item[1][1],
                    average_length,
                ),
                -item[0],
            ),
            reverse=True,
        )
        return [item[0] for _position, item in ranked]

    results: list[MemoryCitation] = []
    used: set[str] = set()
    for group in (_ordered(named), _ordered(other)):
        for page in group:
            key = page.citation.entry_version_id
            if key in used:
                continue
            used.add(key)
            results.append(page.citation)
            if len(results) >= limit:
                return tuple(results)
    return tuple(results)
