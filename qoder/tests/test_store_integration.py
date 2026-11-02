"""集成测试：真实 seekdb 导入、检索、研究记忆与跨运行时复用。"""

from __future__ import annotations

import asyncio

from paperlens.config import PACKAGE_ROOT, PAPERS_DIR, Paths
from paperlens.planner import plan_query
from paperlens.retriever import retrieve
from paperlens.store import CHUNK_KIND, open_store


def _test_paths(tmp_path) -> Paths:
    state = tmp_path / "state"
    return Paths(
        root=PACKAGE_ROOT,
        papers=PAPERS_DIR,
        records=tmp_path / "records",
        site=tmp_path / "site",
        state=state,
        db=state / "seekdb",
        evidence=tmp_path / "evidence",
    )


def test_ingest_search_and_memory_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PAPERLENS_SCOPE_KEY", "paperlens-pytest")
    paths = _test_paths(tmp_path)
    question = "HNSW 的 ef 参数如何影响召回率和查询延迟？"

    async def first_run() -> None:
        async with open_store(paths, reset=True) as store:
            manifest = await store.import_corpus(refresh=True)
            assert manifest["version"] == 1
            assert manifest["paper_total"] == 8
            assert manifest["chunk_total"] >= 50
            assert manifest["manifest_artifact_id"]

            hits = await store.search("HNSW ef 召回率", kind=CHUNK_KIND, limit=5)
            assert hits
            assert any(item["source"] == "hnsw-2018" for item in hits)
            assert all(item["matched_by"] for item in hits)

            selected = await retrieve(store, plan_query(question), top=4)
            assert selected
            assert len({hit.paper_id for hit in selected}) >= 2

            await store.create_note({"type": "confirm", "text": "ef 与召回率正相关"})
            await store.create_note({"type": "reading", "paper": "hnsw-2018", "status": "read"})
            notes = await store.list_notes()
            assert len(notes) == 2
            assert all("seq" in note for note in notes)

    async def second_run() -> None:
        async with open_store(paths) as store:
            manifest = await store.current_manifest()
            assert manifest is not None
            assert manifest["version"] == 1
            assert manifest["paper_total"] == 8
            notes = await store.list_notes()
            assert len(notes) == 2

            detail = await store.get_artifact(manifest["manifest_artifact_id"])
            assert detail["kind"] == "corpus-manifest"

    asyncio.run(first_run())
    asyncio.run(second_run())
