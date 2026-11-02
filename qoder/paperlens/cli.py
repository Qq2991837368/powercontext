"""PaperLens 命令行接口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .config import Paths, load_paths
from .corpus import load_papers
from .evidence import build_pack, render_markdown, save_pack
from .export import export_site
from .memory import apply_note, fold_notes
from .planner import plan_query
from .retriever import retrieve
from .store import open_store

CommandHandler = Callable[[argparse.Namespace, Paths], Awaitable[int]]

_INDEX_HINT = "尚未建立索引，请先运行 python -m paperlens ingest"
_DEFAULT_TOP = 6
_NOTE_KINDS = ("question", "confirm", "exclude", "reading")
_DEFAULT_EXPORT_OUT = "site/web/data/demo.json"


def _positive_int(raw: str) -> int:
    """把命令行参数转换为正整数，失败时抛出 argparse 参数错误。"""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"必须是正整数：{raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"必须是正整数：{raw}")
    return value


def _print_json(payload: Any) -> None:
    """以 UTF-8 友好的缩进 JSON 输出到标准输出。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _as_int(value: Any, default: int = 0) -> int:
    """尽力把值转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """尽力把值转换为浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coverage_ratio(payload: dict[str, Any]) -> float:
    """从证据包或查询日志中提取覆盖率。"""
    direct = payload.get("coverage_ratio")
    if direct is not None:
        return _as_float(direct)
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        for key in ("ratio", "coverage_ratio"):
            if key in coverage:
                return _as_float(coverage[key])
    return 0.0


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """整理清单中的版本、论文数、文本块数与语料指纹。"""
    paper_total = manifest.get("paper_total")
    if paper_total is None:
        papers = manifest.get("papers")
        paper_total = len(papers) if isinstance(papers, (list, dict)) else 0

    chunk_total = manifest.get("chunk_total")
    if chunk_total is None:
        chunks = manifest.get("chunks")
        if isinstance(chunks, bool):
            chunk_total = 0
        elif isinstance(chunks, int):
            chunk_total = chunks
        elif isinstance(chunks, (list, dict)):
            chunk_total = len(chunks)
        else:
            chunk_total = 0

    return {
        "version": manifest.get("version"),
        "paper_total": _as_int(paper_total),
        "chunk_total": _as_int(chunk_total),
        "fingerprint": str(manifest.get("corpus_fingerprint") or ""),
    }


def _memory_items(value: Any) -> list[Any]:
    """把研究记忆字段统一整理为列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _question_text(value: Any) -> str:
    """从研究记忆的 question 字段中提取当前问题文本。"""
    items = _memory_items(value)
    if not items:
        return ""
    return str(items[-1])


def _memory_summary(state: dict[str, Any]) -> dict[str, Any]:
    """汇总研究记忆的当前问题与各类条目数量。"""
    return {
        "question": _question_text(state.get("question")),
        "confirmed": len(_memory_items(state.get("confirmed"))),
        "excluded": len(_memory_items(state.get("excluded"))),
        "excluded_papers": len(_memory_items(state.get("excluded_papers"))),
        "reading": len(_memory_items(state.get("reading"))),
    }


def _excluded_papers(state: dict[str, Any]) -> tuple[str, ...]:
    """提取需要从检索中排除的论文标识。"""
    raw = state.get("excluded_papers") or ()
    if isinstance(raw, (str, bytes)):
        raw = (raw,)
    elif isinstance(raw, dict):
        raw = list(raw.values())

    result: list[str] = []
    for item in raw:
        if item is None:
            continue
        if isinstance(item, dict):
            identifier = item.get("id") or item.get("paper_id") or item.get("paper")
            if identifier is not None:
                result.append(str(identifier))
            continue
        result.append(str(item))
    return tuple(dict.fromkeys(result))


def _note_identity(saved: dict[str, Any], fallback_kind: str) -> tuple[Any, Any]:
    """从创建结果中取出序号与类型，兼容常见字段名。"""
    seq = saved.get("seq")
    if seq is None:
        seq = saved.get("sequence")
    if seq is None:
        seq = saved.get("id")
    if seq is None:
        seq = ""
    note_type = saved.get("type")
    if note_type is None:
        note_type = saved.get("kind")
    if note_type is None:
        note_type = fallback_kind
    return seq, note_type


def _build_parser() -> argparse.ArgumentParser:
    """构造包含全部子命令的命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="paperlens",
        description="PaperLens 科研文献证据库命令行工具。",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="命令",
        help="可用子命令",
    )

    ingest = subparsers.add_parser(
        "ingest",
        help="导入文献并建立检索索引",
        description="导入文献语料，必要时刷新索引。",
    )
    ingest.add_argument("--refresh", action="store_true", help="忽略缓存并强制重建索引")
    ingest.add_argument("--json", action="store_true", help="以 JSON 输出索引清单")
    ingest.set_defaults(handler=_cmd_ingest)

    ask = subparsers.add_parser(
        "ask",
        help="针对研究问题检索证据",
        description="根据问题检索文献片段，生成证据包并可保存。",
    )
    ask.add_argument("question", help="要检索的研究问题")
    ask.add_argument(
        "--top",
        type=_positive_int,
        default=_DEFAULT_TOP,
        metavar="N",
        help=f"返回的文本块数量（默认 {_DEFAULT_TOP}）",
    )
    ask.add_argument("--json", action="store_true", help="以 JSON 输出证据包")
    ask.add_argument("--no-save", action="store_true", help="不保存证据包文件")
    ask.set_defaults(handler=_cmd_ask)

    note = subparsers.add_parser(
        "note",
        help="记录研究记忆",
        description="创建问题、确认、排除或待读等研究记忆条目。",
    )
    note.add_argument(
        "--kind",
        required=True,
        choices=_NOTE_KINDS,
        help="记忆类型：question/confirm/exclude/reading",
    )
    note.add_argument("--text", default="", help="记忆正文")
    note.add_argument("--paper", default=None, help="关联的论文标识")
    note.add_argument("--status", default=None, help="附加状态")
    note.set_defaults(handler=_cmd_note)

    status = subparsers.add_parser(
        "status",
        help="查看索引与研究记忆状态",
        description="显示索引清单、研究记忆摘要和最近查询记录。",
    )
    status.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    status.set_defaults(handler=_cmd_status)

    export_site_parser = subparsers.add_parser(
        "export-site",
        help="导出演示站点数据",
        description="运行示例问题并生成站点所需的 demo.json。",
    )
    export_site_parser.add_argument(
        "--out",
        default=_DEFAULT_EXPORT_OUT,
        metavar="路径",
        help=f"输出文件路径（默认 {_DEFAULT_EXPORT_OUT}）",
    )
    export_site_parser.set_defaults(handler=_cmd_export_site)

    return parser


async def _cmd_ingest(args: argparse.Namespace, paths: Paths) -> int:
    """执行 ingest 子命令。"""
    async with open_store(paths) as store:
        manifest = await store.import_corpus(refresh=bool(args.refresh))

    if args.json:
        _print_json(manifest)
        return 0

    summary = _manifest_summary(manifest)
    print("索引已更新")
    print(f"版本：{summary['version']}")
    print(f"论文数：{summary['paper_total']}")
    print(f"文本块数：{summary['chunk_total']}")
    print(f"语料指纹：{summary['fingerprint']}")
    return 0


async def _cmd_ask(args: argparse.Namespace, paths: Paths) -> int:
    """执行 ask 子命令。"""
    question = str(args.question).strip()
    if not question:
        raise ValueError("问题不能为空")

    evidence_path: str | None = None
    async with open_store(paths) as store:
        manifest = await store.current_manifest()
        if not manifest:
            print(_INDEX_HINT, file=sys.stderr)
            return 1

        notes = await store.list_notes()
        state = fold_notes(notes)
        plan = plan_query(question)
        hits = await retrieve(
            store,
            plan,
            top=args.top,
            exclude_papers=_excluded_papers(state),
        )
        papers = load_papers(paths.papers)
        pack = build_pack(
            question,
            plan,
            hits,
            papers=papers,
            memory_state=state,
            manifest=manifest,
            scope_id=store.scope_id,
        )

        if not args.no_save:
            evidence_path = str(save_pack(paths, pack))

        coverage_ratio = _coverage_ratio(pack)
        await store.log_query(
            {
                "question": question,
                "top": args.top,
                "hits": len(hits),
                "coverage_ratio": coverage_ratio,
                "evidence_path": evidence_path,
            }
        )

    if args.json:
        _print_json(pack)
        if evidence_path:
            print(f"证据包已保存：{evidence_path}", file=sys.stderr)
        return 0

    print(render_markdown(pack).rstrip())
    if evidence_path:
        print(f"证据包已保存：{evidence_path}")
    else:
        print("已跳过保存证据包")
    return 0


async def _cmd_note(args: argparse.Namespace, paths: Paths) -> int:
    """执行 note 子命令。"""
    kind = str(args.kind)
    text = str(args.text or "").strip()

    async with open_store(paths) as store:
        notes = await store.list_notes()
        event = apply_note(
            notes,
            kind=kind,
            text=text,
            paper=args.paper,
            status=args.status,
        )
        saved = await store.create_note(event)

    seq, note_type = _note_identity(saved, kind)
    print(f"已记录研究记忆：seq={seq} type={note_type}")
    return 0


async def _cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    """执行 status 子命令。"""
    async with open_store(paths) as store:
        manifest = await store.current_manifest()
        notes = await store.list_notes()
        logs = await store.list_query_logs(5)

    memory = _memory_summary(fold_notes(notes))
    if manifest:
        corpus = _manifest_summary(manifest)
    else:
        corpus = {
            "version": None,
            "paper_total": 0,
            "chunk_total": 0,
            "fingerprint": "",
        }

    recent_queries: list[dict[str, Any]] = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        recent_queries.append(
            {
                "question": str(log.get("question") or ""),
                "coverage_ratio": _coverage_ratio(log),
            }
        )

    payload: dict[str, Any] = {
        "indexed": manifest is not None,
        "manifest": corpus,
        "memory": memory,
        "recent_queries": recent_queries,
    }

    if args.json:
        _print_json(payload)
        return 0

    print("索引状态")
    if manifest:
        print(f"  版本：{corpus['version']}")
        print(f"  论文数：{corpus['paper_total']}")
        print(f"  文本块数：{corpus['chunk_total']}")
        print(f"  语料指纹：{corpus['fingerprint']}")
    else:
        print(f"  {_INDEX_HINT}")

    print("研究记忆")
    print(f"  当前问题：{memory['question'] or '（无）'}")
    print(f"  已确认：{memory['confirmed']} 条")
    print(f"  已排除：{memory['excluded']} 条")
    print(f"  排除论文：{memory['excluded_papers']} 篇")
    print(f"  待读：{memory['reading']} 条")

    print("最近查询")
    if recent_queries:
        for item in recent_queries:
            question = item["question"] or "（空问题）"
            print(f"  - {question}  覆盖率={item['coverage_ratio']:.2f}")
    else:
        print("  （暂无查询记录）")
    return 0


async def _cmd_export_site(args: argparse.Namespace, paths: Paths) -> int:
    """执行 export-site 子命令。"""
    out = Path(str(args.out)).expanduser()
    if not out.is_absolute():
        out = paths.root / out

    async with open_store(paths) as store:
        payload = await export_site(store, paths, out=out)

    cases = payload.get("cases")
    case_count = len(cases) if isinstance(cases, list) else 0
    print(f"已生成 {case_count} 个演示案例")
    print(f"输出路径：{out}")

    mirror = paths.site / "web" / "data" / "demo.json"
    if mirror.resolve() != out.resolve():
        print(f"站点数据：{mirror}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """运行 PaperLens 命令行入口并返回进程退出码。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    paths = load_paths()
    try:
        paths.ensure()
        handler: CommandHandler = args.handler
        return asyncio.run(handler(args, paths))
    except (ValueError, FileNotFoundError) as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        print(f"错误：{detail}", file=sys.stderr)
        return 1
