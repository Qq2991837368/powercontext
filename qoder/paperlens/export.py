"""导出 PaperLens 演示站点所需的数据。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Paths
from .corpus import load_papers
from .evidence import build_pack, save_pack
from .memory import fold_notes
from .planner import plan_query
from .retriever import retrieve

_DEMO_QUESTIONS = Path("corpus") / "demo-questions.json"
_SITE_DATA = Path("web") / "data" / "demo.json"


async def export_site(
    store: Any,
    paths: Paths,
    *,
    out: Path,
    top: int = 6,
) -> dict[str, Any]:
    """生成演示站点数据，写入 ``out`` 并同步到站点目录。

    参数：
        store：已打开的 PaperLens 存储对象。
        paths：运行期路径集合。
        out：输出 JSON 文件路径；相对路径以项目根目录为基准。
        top：默认检索返回的文本块数量，可被单个案例的 ``top`` 覆盖。

    返回：
        组装后的站点数据字典。
    """
    if top <= 0:
        raise ValueError("top 必须是正整数")

    manifest = await store.current_manifest()
    if not manifest:
        raise ValueError("尚未建立索引，请先运行 python -m paperlens ingest")

    notes = await store.list_notes()
    memory_state = fold_notes(notes)
    papers = load_papers(paths.papers)
    case_items = _load_cases(paths.root / _DEMO_QUESTIONS)
    scope_id = store.scope_id
    chunk_counts = _paper_chunk_counts(manifest)

    cases: list[dict[str, Any]] = []
    for index, case in enumerate(case_items, start=1):
        case_id = str(case.get("id") or f"case-{index}")
        question = str(case.get("question") or "").strip()
        if not question:
            raise ValueError(f"示例问题 {case_id} 缺少 question 字段")
        case_top = _case_top(case, top)
        plan = plan_query(question)
        hits = await retrieve(store, plan, top=case_top)
        pack = build_pack(
            question,
            plan,
            hits,
            papers=papers,
            memory_state=memory_state,
            manifest=manifest,
            scope_id=scope_id,
        )
        save_pack(paths, pack)
        cases.append(
            {
                "id": case_id,
                "title": str(case.get("title") or question),
                "question": question,
                "pack": pack,
            }
        )

    paper_list: list[dict[str, Any]] = []
    for paper in papers:
        meta = paper.meta
        paper_id = str(getattr(meta, "id", "") or "")
        paper_list.append(
            {
                "id": paper_id,
                "title": str(getattr(meta, "title", "") or ""),
                "authors": _as_list(getattr(meta, "authors", None)),
                "year": getattr(meta, "year", None),
                "venue": str(getattr(meta, "venue", "") or ""),
                "tags": _as_list(getattr(meta, "tags", None)),
                "chunks": chunk_counts.get(paper_id, 0),
            }
        )

    paper_total = _as_int(manifest.get("paper_total"), len(papers))
    chunk_total = _as_int(manifest.get("chunk_total"), sum(chunk_counts.values()))

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope_id": scope_id,
        "corpus": {
            "papers": paper_total,
            "chunks": chunk_total,
            "fingerprint": str(manifest.get("corpus_fingerprint") or ""),
            "manifest_version": manifest.get("version"),
            "paper_list": paper_list,
        },
        "memory": memory_state,
        "cases": cases,
        "records": {
            "db_queries": _count_lines(paths.records / "db-queries.jsonl"),
            "evidence_packs": _count_files(paths.evidence, pattern="*.json"),
            "qoder_sessions": _count_files(paths.records / "qoder-sessions"),
        },
    }

    destination = out.expanduser()
    if not destination.is_absolute():
        destination = paths.root / destination
    if destination.exists() and destination.is_dir():
        raise ValueError(f"输出路径是目录：{destination}")

    _write_json(destination, payload)

    mirror = paths.site / _SITE_DATA
    if destination.resolve() != mirror.resolve():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(destination, mirror)

    return payload


def _load_cases(path: Path) -> list[dict[str, Any]]:
    """读取示例问题列表，兼容直接数组与带 cases/questions 的对象。"""
    if not path.is_file():
        raise FileNotFoundError(f"未找到示例问题文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"示例问题文件不是合法 JSON：{path}") from exc

    if isinstance(data, dict):
        for key in ("cases", "questions"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break

    if not isinstance(data, list):
        raise ValueError(f"示例问题文件格式无效：{path}")

    cases: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            cases.append(item)
    return cases


def _case_top(case: dict[str, Any], default: int) -> int:
    """取单个案例的 top，非法值回退到默认值。"""
    raw = case.get("top")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _as_int(value: Any, default: int = 0) -> int:
    """尽力把值转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    """把标量或序列统一为列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _chunk_count(value: Any) -> int | None:
    """从多种清单结构中推断单个论文的文本块数量。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        for key in ("chunk_count", "chunk_total", "chunks", "count"):
            if key in value:
                count = _chunk_count(value[key])
                if count is not None:
                    return count
    return None


def _manifest_papers(value: Any) -> list[tuple[str, dict[str, Any]]]:
    """把清单中的 papers 字段整理为 ``(论文 ID, 条目)`` 列表。"""
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                entries.append((str(key), item))
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id") or item.get("paper_id") or item.get("paper")
            if identifier is not None:
                entries.append((str(identifier), item))
    return entries


def _paper_chunk_counts(manifest: dict[str, Any]) -> dict[str, int]:
    """从清单中提取每篇论文的文本块数量。"""
    counts: dict[str, int] = {}

    mapping = manifest.get("paper_chunks")
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            count = _chunk_count(value)
            if count is not None:
                counts[str(key)] = count

    for paper_id, entry in _manifest_papers(manifest.get("papers")):
        count = _chunk_count(entry)
        if count is not None:
            counts.setdefault(paper_id, count)

    return counts


def _count_lines(path: Path) -> int:
    """统计 JSONL 文件中的非空行数。"""
    if not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def _count_files(directory: Path, *, pattern: str | None = None) -> int:
    """统计目录下的文件数；未指定模式时递归统计。"""
    if not directory.is_dir():
        return 0
    items = directory.glob(pattern) if pattern else directory.rglob("*")
    return sum(1 for item in items if item.is_file())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8 写入缩进 JSON，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
