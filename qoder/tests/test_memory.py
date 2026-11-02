"""研究记忆测试：事件校验与折叠规则。"""

from __future__ import annotations

import pytest

from paperlens.memory import apply_note, fold_notes


def test_apply_note_rejects_invalid_input() -> None:
    notes: list[dict[str, object]] = []
    with pytest.raises(ValueError):
        apply_note(notes, kind="unknown", text="x")
    with pytest.raises(ValueError):
        apply_note(notes, kind="confirm", text="   ")
    with pytest.raises(ValueError):
        apply_note(notes, kind="reading", status="read")
    with pytest.raises(ValueError):
        apply_note(notes, kind="reading", paper="hnsw-2018", status="done")


def test_fold_notes_collapses_events_in_seq_order() -> None:
    notes = [
        {"seq": 3, "type": "confirm", "text": "PQ 适合内存导航层"},
        {"seq": 1, "type": "question", "text": "内存受限如何做十亿级检索"},
        {"seq": 2, "type": "confirm", "text": "PQ 适合内存导航层"},
        {"seq": 4, "type": "exclude", "text": "暂不考虑纯 GPU 方案"},
        {"seq": 5, "type": "exclude", "text": "暂不考虑纯 GPU 方案", "paper": "faiss-gpu-2019"},
        {"seq": 6, "type": "reading", "paper": "hnsw-2018", "status": "read"},
        {"seq": 7, "type": "reading", "paper": "hnsw-2018", "status": "skim"},
    ]
    folded = fold_notes(notes)
    assert folded["question"] == "内存受限如何做十亿级检索"
    assert folded["confirmed"] == ["PQ 适合内存导航层"]
    assert folded["excluded"][0] == {"text": "暂不考虑纯 GPU 方案", "paper": None}
    assert "faiss-gpu-2019" in folded["excluded_papers"]
    assert folded["reading"] == {"hnsw-2018": "skim"}


def test_fold_notes_without_seq_uses_input_order() -> None:
    folded = fold_notes([
        {"type": "question", "text": "第一个问题"},
        {"type": "question", "text": "第二个问题"},
    ])
    assert folded["question"] == "第二个问题"
    assert folded["confirmed"] == []
    assert folded["excluded_papers"] == []
