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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_MIN_EVIDENCE_FRAGMENT_CHARS = 16
_MAX_EVIDENCE_DOCUMENTS = 4
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_Item = TypeVar("_Item")
_TERM_PATTERN = re.compile(r"[a-z0-9]+")
_SENTENCE_END_PATTERN = re.compile(r"[.!?]+[\"')\]]*(?=\s|$)")
_SENTENCE_ABBREVIATIONS = frozenset({
    "al",
    "approx",
    "ca",
    "cf",
    "corp",
    "dept",
    "dr",
    "e.g",
    "eq",
    "eqs",
    "etc",
    "fig",
    "figs",
    "i.e",
    "inc",
    "ltd",
    "mr",
    "mrs",
    "ms",
    "no",
    "nos",
    "pp",
    "prof",
    "ref",
    "refs",
    "st",
    "u.k",
    "u.s",
    "u.s.a",
    "vs",
})
_QUERY_STOPWORDS = frozenset({
    "a",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "here",
    "in",
    "is",
    "it",
    "its",
    "may",
    "might",
    "more",
    "most",
    "not",
    "of",
    "on",
    "or",
    "our",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "this",
    "those",
    "to",
    "using",
    "via",
    "was",
    "we",
    "were",
    "which",
    "who",
    "whom",
    "with",
})
_BM25_K1 = 1.2
_BM25_B = 0.75


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class _EvidenceSentence:
    start: int
    end: int


@dataclass(frozen=True)
class _EvidenceDocument:
    address: MemoryEntryAddress
    text: str
    sentences: tuple[_EvidenceSentence, ...]


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[PreparedContextFragment, ...] = ()


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedContextFragment:
    """One literal span selected out of an exact Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope."""

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 16
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    entry_limit = 8
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        self._validate_candidate_limits(memory_candidates, topic_memory_hits, experience_candidates)
        if request.evidence_selection:
            return self._select_evidence_result(
                request=request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
            )
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
        )

    def _validate_candidate_limits(
        self,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
    ) -> None:
        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

    def _select_evidence_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
    ) -> PreparedContextBuild:
        """Select literal Memory spans by query relevance under the full output budget."""

        documents = self._evidence_documents(memory_candidates, current_scope_id)
        if not documents:
            return PreparedContextBuild(context=self.empty(), origins=())

        query_terms = _query_terms(request.query)
        scores = _evidence_scores(documents, query_terms)
        sentence_count = {index: len(document.sentences) for index, document in enumerate(documents)}
        document_scores = {
            index: max((scores[(index, sentence)] for sentence in range(count)), default=0.0)
            for index, count in sentence_count.items()
        }
        ranked_documents = sorted(
            (index for index in range(len(documents)) if sentence_count[index]),
            key=lambda index: (-document_scores[index], index),
        )
        relevant_documents = [
            index for index in ranked_documents if document_scores[index] > 0.0
        ][: _MAX_EVIDENCE_DOCUMENTS]

        selected: dict[int, set[int]] = {index: set() for index in range(len(documents))}
        extras: dict[int, list[tuple[int, int]]] = {index: [] for index in range(len(documents))}

        # Rank one best sentence per relevant document first, then everything else by score.
        candidates: list[tuple[int, int]] = []
        best_keys: set[tuple[int, int]] = set()
        for index in relevant_documents:
            best = max(
                range(sentence_count[index]),
                key=lambda sentence: (scores[(index, sentence)], -documents[index].sentences[sentence].start),
            )
            candidates.append((index, best))
            best_keys.add((index, best))
        rank_position = {index: position for position, index in enumerate(ranked_documents)}
        candidates.extend(
            sorted(
                (
                    (index, sentence)
                    for index, document in enumerate(documents)
                    for sentence in range(len(document.sentences))
                    if (index, sentence) not in best_keys and scores[(index, sentence)] > 0.0
                ),
                key=lambda key: (
                    -scores[key],
                    rank_position.get(key[0], len(rank_position)),
                    documents[key[0]].sentences[key[1]].start,
                ),
            )
        )

        def current() -> tuple[tuple[PreparedContextFragment, ...], tuple[_PreparedContextEntry, ...]]:
            return _evidence_entries(documents, selected, extras)

        def fits(entries: Sequence[_PreparedContextEntry]) -> bool:
            return len(_render(entries).encode("utf-8")) <= request.max_bytes

        added = False
        deferred: list[tuple[int, int]] = []
        for index, sentence in candidates:
            selected[index].add(sentence)
            _, entries = current()
            if fits(entries):
                added = True
            else:
                selected[index].discard(sentence)
                deferred.append((index, sentence))

        if not added and not candidates:
            # Nothing in the query matched any candidate: fall back to the opening literal text.
            for index in ranked_documents:
                selected[index].add(0)
                _, entries = current()
                if fits(entries):
                    added = True
                    break
                selected[index].discard(0)

        if deferred:
            # Use the leftover bytes for the highest-ranked span that did not fit whole.
            query_vocabulary = set(query_terms)
            for index, sentence in deferred:
                span = documents[index].sentences[sentence]
                lower = span.start + _MIN_EVIDENCE_FRAGMENT_CHARS
                if lower >= span.end:
                    continue
                best_end: int | None = None
                low, high = lower, span.end
                while low <= high:
                    middle = (low + high) // 2
                    extras[index].append((span.start, middle))
                    _, entries = current()
                    fits_now = fits(entries)
                    extras[index].pop()
                    if fits_now:
                        best_end = middle
                        low = middle + 1
                    else:
                        high = middle - 1
                if best_end is None:
                    continue
                piece = documents[index].text[span.start:best_end]
                if query_vocabulary and not query_vocabulary.intersection(_tokens(piece)):
                    continue
                extras[index].append((span.start, best_end))
                break

        fragments, entries = current()
        if not fragments:
            return PreparedContextBuild(context=self.empty(), origins=())
        content = _render(entries)
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=tuple(entry.origin for entry in entries),
            fragments=fragments,
        )

    def _evidence_documents(
        self,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        current_scope_id: str | None,
    ) -> tuple[_EvidenceDocument, ...]:
        documents: list[_EvidenceDocument] = []
        seen: set[tuple[str, str, str, int, str, str]] = set()
        for candidates in memory_candidates:
            if candidates.hits and candidates.memory_ref is None:
                raise PreparedContextInvariantError("memory-ref-missing")
            scope_id = candidates.scope_id or current_scope_id or ""
            for hit in candidates.hits:
                if hit.memory_ref != candidates.memory_ref:
                    raise PreparedContextInvariantError("memory-ref-mismatch")
                if (
                    not hit.entry_id.strip()
                    or not hit.entry_version_id.strip()
                    or not hit.text.strip()
                    or not scope_id
                ):
                    continue
                identity = (
                    scope_id,
                    hit.memory_ref.family,
                    hit.memory_ref.artifact_id,
                    hit.memory_ref.revision,
                    hit.entry_id,
                    hit.entry_version_id,
                )
                if identity in seen:
                    continue
                seen.add(identity)
                address = MemoryEntryAddress(
                    memory=ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref),
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                documents.append(
                    _EvidenceDocument(
                        address=address,
                        text=hit.text,
                        sentences=tuple(
                            _EvidenceSentence(start=start, end=end) for start, end in _sentence_spans(hit.text)
                        ),
                    )
                )
        return tuple(documents)

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions)``; ``content`` is ``None`` exactly when nothing
        was selected, which is the empty-result case for both paths. This is the single
        selection path shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the
        probe's counters always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            return self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )

        memory_entries = _interleave_groups(
            tuple(
                self._memory_entries(
                    candidates.memory_ref,
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in memory_candidates
            )
        )
        topic_memory_entries = self._topic_memory_entries(topic_memory_hits)
        experience_entries = _interleave_groups(
            tuple(
                self._experience_entries(
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in experience_candidates
            )
        )[: self.experience_entry_limit]
        entries, omissions = self._fit_entries(request, memory_entries, topic_memory_entries, experience_entries)
        if not entries:
            return None, (), omissions
        return _render(entries), tuple(entry.origin for entry in entries), omissions

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            for entry in entries:
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                rank += 1
                outcome = fit_context_text_item(included, replace(item, recall_rank=rank), assembly, request.max_bytes)
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                selected_count += 1
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for candidate in _interleave(memory_entries, topic_memory_entries, experience_entries):
            if len(entries) >= self.entry_limit:
                break
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=request.max_bytes,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate)) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate)) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _evidence_entries(
    documents: Sequence[_EvidenceDocument],
    selected: dict[int, set[int]],
    extras: dict[int, list[tuple[int, int]]],
) -> tuple[tuple[PreparedContextFragment, ...], tuple[_PreparedContextEntry, ...]]:
    fragments: list[PreparedContextFragment] = []
    entries: list[_PreparedContextEntry] = []
    for index, document in enumerate(documents):
        for start, end in _merged_spans(document, selected[index], extras[index]):
            text = document.text[start:end]
            if not text.strip():
                continue
            address = document.address
            citation = {
                "scope_id": address.memory.scope_id,
                "memory_ref": address.memory.artifact.model_dump(mode="json"),
                "entry_id": address.entry_id,
                "entry_version_id": address.entry_version_id,
            }
            fragments.append(PreparedContextFragment(address=address, start=start, end=end, text=text))
            entries.append(
                _PreparedContextEntry(
                    origin=address,
                    kind="memory",
                    citation=citation,
                    content=text,
                    truncated=False,
                    start=start,
                    end=end,
                )
            )
    return tuple(fragments), tuple(entries)


def _merged_spans(
    document: _EvidenceDocument,
    selected: set[int],
    extras: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    spans = [(document.sentences[index].start, document.sentences[index].end) for index in sorted(selected)]
    spans.extend(extras)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if start >= end:
            continue
        if merged:
            previous_start, previous_end = merged[-1]
            if start <= previous_end or not document.text[previous_end:start].strip():
                merged[-1] = (previous_start, max(previous_end, end))
                continue
        merged.append((start, end))
    return tuple(merged)


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    line_start = 0
    for line in text.split("\n"):
        line_end = line_start + len(line)
        spans.extend(_line_sentence_spans(text, line_start, line_end))
        line_start = line_end + 1
    return tuple(spans)


def _line_sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in _SENTENCE_END_PATTERN.finditer(text, start, end):
        stop = match.end()
        index = stop
        while index < end and text[index].isspace():
            index += 1
        if index < end:
            next_char = text[index]
            if next_char.isalpha() and next_char.islower():
                continue
            if next_char in "([{" and index + 1 < end and text[index + 1].islower():
                continue
        if _is_sentence_abbreviation(text, match.start(), start):
            continue
        span = _trimmed_span(text, cursor, stop)
        if span is not None:
            spans.append(span)
        cursor = stop
    span = _trimmed_span(text, cursor, end)
    if span is not None:
        spans.append(span)
    return spans


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return start, end


def _is_sentence_abbreviation(text: str, stop: int, line_start: int) -> bool:
    index = stop
    while index > line_start and (text[index - 1].isalnum() or text[index - 1] in ".-"):
        index -= 1
    word = text[index:stop].lower().strip("\"'()[]")
    if word in _SENTENCE_ABBREVIATIONS:
        return True
    return len(word) == 1 and word.isalpha() and word != "i"


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TERM_PATTERN.findall(text.lower()))


def _stem(token: str) -> str:
    """Normalize common English inflections so claims can meet their evidence wording."""

    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith(("ches", "shes", "xes", "zes", "ses")) and len(token) > 4:
        token = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        token = token[:-1]
    if token.endswith("ied") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("ing") and len(token) > 5:
        token = token[:-3]
    elif token.endswith("ed") and len(token) > 4:
        token = token[:-2]
    elif token.endswith("ion") and len(token) > 5:
        token = token[:-3]
    if token.endswith("e") and len(token) > 4:
        token = token[:-1]
    if len(token) > 3 and token[-1] == token[-2] and token[-1].isalpha():
        token = token[:-1]
    return token


def _query_terms(query: str) -> tuple[str, ...]:
    terms = _tokens(query)
    meaningful = tuple(term for term in terms if term not in _QUERY_STOPWORDS)
    return tuple(dict.fromkeys(meaningful or terms))


def _evidence_scores(
    documents: Sequence[_EvidenceDocument],
    query_terms: Sequence[str],
) -> dict[tuple[int, int], float]:
    rows: list[tuple[int, int, tuple[str, ...]]] = []
    for document_index, document in enumerate(documents):
        for sentence_index, sentence in enumerate(document.sentences):
            tokens = _tokens(document.text[sentence.start : sentence.end])
            rows.append((document_index, sentence_index, tokens))
    if not rows:
        return {}
    stems = [tuple(_stem(term) for term in tokens) for _, _, tokens in rows]
    document_frequency = Counter(term for stemmed in stems for term in set(stemmed))
    total = len(rows)
    average_length = sum(len(stemmed) for stemmed in stems) / total or 1.0
    raw_query = tuple(dict.fromkeys(query_terms))
    query = tuple(dict.fromkeys(_stem(term) for term in raw_query))
    inverse_frequency = {
        term: math.log(
            1.0 + (total - document_frequency.get(term, 0) + 0.5) / (document_frequency.get(term, 0) + 0.5)
        )
        for term in query
    }
    total_inverse_frequency = sum(inverse_frequency.values()) or 1.0
    phrase = " ".join(raw_query)
    vocabulary = set(query)
    scores: dict[tuple[int, int], float] = {}
    for row_index, (document_index, sentence_index, tokens) in enumerate(rows):
        frequencies = Counter(stems[row_index])
        length = len(tokens)
        score = 0.0
        for term in query:
            frequency = frequencies.get(term, 0)
            if frequency:
                score += inverse_frequency[term] * (frequency * (_BM25_K1 + 1.0)) / (
                    frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / average_length)
                )
        if phrase and phrase in " ".join(tokens):
            score += 0.5 * total_inverse_frequency + 1.0
        covered = len(vocabulary.intersection(stems[row_index]))
        if covered:
            score += 0.1 * total_inverse_frequency * covered / len(query)
        scores[(document_index, sentence_index)] = score
    return scores


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(entries: Sequence[_PreparedContextEntry]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry) for entry in entries],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": entry.citation,
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.start is not None and entry.end is not None:
        rendered["start"] = entry.start
        rendered["end"] = entry.end
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _rendered_bytes(entries: Sequence[_PreparedContextEntry]) -> int:
    return len(_render(entries).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"
