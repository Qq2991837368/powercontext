"""语料契约测试：front matter、切块粒度、唯一性与指纹稳定性。"""

from __future__ import annotations

from paperlens.config import PAPERS_DIR
from paperlens.corpus import MAX_CHUNK_CHARS, corpus_fingerprint, load_papers


def test_corpus_has_expected_papers_and_chunks() -> None:
    papers = load_papers(PAPERS_DIR)
    assert len(papers) == 8
    ids = [paper.meta.id for paper in papers]
    assert len(set(ids)) == len(ids)
    total_chunks = sum(len(paper.chunks) for paper in papers)
    assert total_chunks >= 50


def test_every_paper_has_five_sections_and_valid_meta() -> None:
    for paper in load_papers(PAPERS_DIR):
        assert paper.meta.title
        assert paper.meta.authors
        assert 2000 <= paper.meta.year <= 2030
        assert paper.meta.venue
        assert paper.meta.tags
        sections = {chunk.section for chunk in paper.chunks}
        assert len(sections) == 5, paper.meta.id

    chunk_ids: set[str] = set()
    for paper in load_papers(PAPERS_DIR):
        for chunk in paper.chunks:
            assert chunk.chunk_id not in chunk_ids
            chunk_ids.add(chunk.chunk_id)
            assert chunk.text.startswith(f"## {chunk.section}")
            body = chunk.text.split("\n", 1)[1]
            assert body.strip()
            assert len(body) <= MAX_CHUNK_CHARS, (chunk.chunk_id, len(body))


def test_fingerprint_is_order_independent() -> None:
    papers = load_papers(PAPERS_DIR)
    assert corpus_fingerprint(papers) == corpus_fingerprint(tuple(reversed(papers)))
