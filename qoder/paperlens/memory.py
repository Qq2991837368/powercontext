"""PaperLens 研究记忆：事件校验与折叠。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_NOTE_KINDS: frozenset[str] = frozenset({"question", "confirm", "exclude", "reading"})
_READING_STATUSES: frozenset[str] = frozenset({"read", "skim", "todo"})


def apply_note(
    notes: list[dict[str, Any]],
    *,
    kind: str,
    text: str | None = None,
    paper: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """校验并追加一条研究记忆事件。

    ``kind`` 只能是 ``question``、``confirm``、``exclude`` 或 ``reading``。
    ``reading`` 事件必须提供非空 ``paper``，且 ``status`` 属于
    ``read``、``skim``、``todo``；其余事件必须提供非空 ``text``。

    Args:
        notes: 事件列表，函数会原地追加新事件。
        kind: 事件类型。
        text: 事件文本。
        paper: 关联的论文 id。
        status: 阅读状态，仅 ``reading`` 事件使用。

    Returns:
        追加后的事件字典。

    Raises:
        ValueError: 类型、文本、论文或阅读状态不合法时抛出。
    """
    if kind not in _NOTE_KINDS:
        raise ValueError(
            f"未知的记忆类型：{kind!r}，仅支持 question、confirm、exclude、reading。"
        )

    if kind == "reading":
        if not isinstance(paper, str) or not paper.strip():
            raise ValueError("reading 事件必须提供非空的 paper。")
        if status not in _READING_STATUSES:
            raise ValueError(
                f"reading 事件的 status 必须是 read、skim 或 todo，收到 {status!r}。"
            )
    elif not isinstance(text, str) or not text.strip():
        raise ValueError(f"{kind} 事件必须提供非空的 text。")

    event: dict[str, Any] = {
        "type": kind,
        "text": text or "",
        "paper": paper,
        "status": status,
    }
    notes.append(event)
    return event


def fold_notes(notes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """按 ``seq`` 升序折叠研究记忆事件。

    缺少整数 ``seq`` 的事件按传入顺序参与排序。返回值中的列表均保持
    首次出现顺序并去重。

    Args:
        notes: 研究记忆事件序列。

    Returns:
        折叠后的记忆状态字典，包含当前问题、已确认项、已排除项、
        已排除论文和阅读进度。
    """
    ordered: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, event in enumerate(notes):
        raw_seq = event.get("seq")
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else index
        ordered.append((seq, index, event))
    ordered.sort(key=lambda item: (item[0], item[1]))

    question: str | None = None
    confirmed: list[str] = []
    excluded: list[dict[str, str | None]] = []
    excluded_papers: list[str] = []
    reading: dict[str, str] = {}

    seen_confirmed: set[str] = set()
    seen_excluded: set[tuple[str, str | None]] = set()
    seen_papers: set[str] = set()

    for _, _, event in ordered:
        kind = event.get("type")
        text = _as_text(event.get("text"))
        paper = _as_optional_text(event.get("paper"))

        if kind == "question":
            question = text
        elif kind == "confirm":
            if text and text not in seen_confirmed:
                seen_confirmed.add(text)
                confirmed.append(text)
        elif kind == "exclude":
            key = (text, paper)
            if key not in seen_excluded:
                seen_excluded.add(key)
                excluded.append({"text": text, "paper": paper})
            if paper is not None and paper not in seen_papers:
                seen_papers.add(paper)
                excluded_papers.append(paper)
        elif kind == "reading" and paper is not None:
            status = _as_optional_text(event.get("status"))
            if status is not None:
                reading[paper] = status

    return {
        "question": question,
        "confirmed": confirmed,
        "excluded": excluded,
        "excluded_papers": excluded_papers,
        "reading": reading,
    }


def _as_text(value: Any) -> str:
    """把事件字段规范化为字符串。"""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_optional_text(value: Any) -> str | None:
    """把可选事件字段规范化为非空字符串或 ``None``。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else None
