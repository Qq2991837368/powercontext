"""Command line interface for the SDD context module."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .config import load_paths
from .context import build_context
from .solve import solve
from .store import open_store


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _init(args: argparse.Namespace) -> int:
    paths = load_paths()

    async def run() -> dict[str, Any]:
        async with open_store(paths, reset=args.reset) as store:
            manifest = await store.import_corpus(refresh=args.refresh)
            return {
                "status": "ok",
                "scope_id": store.scope_id,
                "database": str(paths.db),
                "corpus_version": manifest["version"],
                "files": [item["path"] for item in manifest["files"]],
                "artifact_ids": [item["artifact_id"] for item in manifest["files"]],
                "manifest_artifact_id": manifest["artifact_id"],
            }

    _print(asyncio.run(run()))
    return 0


def _context(args: argparse.Namespace) -> int:
    paths = load_paths()

    async def run() -> dict[str, Any]:
        async with open_store(paths) as store:
            context = await build_context(
                store,
                paths,
                task=args.task,
                editable_file=args.editable_file,
                limit_per_kind=args.limit,
            )
            return {
                "status": "ok",
                "context_id": context["context_id"],
                "corpus_version": context["corpus_version"],
                "state_seq": (context.get("state") or {}).get("seq"),
                "hits": {
                    kind: [hit["source"] for hit in items]
                    for kind, items in context["retrieval"]["hits"].items()
                },
                "files": context["files"],
            }

    _print(asyncio.run(run()))
    return 0


def _record(args: argparse.Namespace) -> int:
    paths = load_paths()
    verification = None
    if args.verification_json:
        verification = json.loads(args.verification_json)

    async def run() -> dict[str, Any]:
        async with open_store(paths) as store:
            state = await store.record_state(
                task=args.task,
                status=args.status,
                summary=args.summary,
                verification=verification,
                notes=args.notes,
            )
            return {"status": "ok", "state": state}

    _print(asyncio.run(run()))
    return 0


def _status(args: argparse.Namespace) -> int:
    paths = load_paths()

    async def run() -> dict[str, Any]:
        async with open_store(paths) as store:
            history = await store.state_history(limit=args.history)
            latest = history[0] if history else None
            manifest = await store.current_manifest()
            return {
                "status": "ok",
                "scope_id": store.scope_id,
                "corpus_version": None if manifest is None else manifest["version"],
                "latest": latest,
                "history": history,
            }

    result = asyncio.run(run())
    if args.json:
        _print(result)
    else:
        latest = result["latest"]
        if latest is None:
            print("No task state stored yet.")
        else:
            print(
                f"latest seq={latest['seq']} status={latest['status']} "
                f"artifact={latest['artifact_id']} corpus=v{result['corpus_version']}"
            )
            print(f"summary: {latest['summary']}")
            if latest.get("verification"):
                print("verification: " + json.dumps(latest["verification"], ensure_ascii=False))
            for item in result["history"][1:]:
                print(f"  seq {item['seq']} [{item['status']}] {item['summary']}")
    return 0


def _solve(args: argparse.Namespace) -> int:
    paths = load_paths()

    async def run() -> dict[str, Any]:
        async with open_store(paths) as store:
            result = await solve(
                store,
                paths,
                task=args.task,
                context_path=Path(args.context),
                editable_file=args.editable_file,
                target_tests=args.target_tests,
                regression_tests=args.regression_tests,
                max_attempts=args.max_attempts,
            )
            result.pop("diff_text", None)
            return result

    _print(asyncio.run(run()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdd-context", description="MiniMax + seekdb SDD context demo")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="import specs/code/tests into seekdb and PowerContext")
    init.add_argument("--reset", action="store_true", help="delete the existing seekdb directory first")
    init.add_argument("--refresh", action="store_true", help="forget the previous corpus before importing")
    init.set_defaults(func=_init)

    context = sub.add_parser("context", help="retrieve specifications/code/tests and stored task state")
    context.add_argument("--task", required=True)
    context.add_argument("--editable-file", default="src/book_filter.py")
    context.add_argument("--limit", type=int, default=3)
    context.set_defaults(func=_context)

    record = sub.add_parser("record", help="append a task state record")
    record.add_argument("--task", required=True)
    record.add_argument("--status", required=True, choices=("planned", "in_progress", "done", "failed"))
    record.add_argument("--summary", required=True)
    record.add_argument("--verification-json", default=None)
    record.add_argument("--notes", default=None)
    record.set_defaults(func=_record)

    status = sub.add_parser("status", help="read the latest state in a fresh process")
    status.add_argument("--history", type=int, default=5)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status)

    solve_cmd = sub.add_parser("solve", help="ask MiniMax to change code, then verify and persist state")
    solve_cmd.add_argument("--task", required=True)
    solve_cmd.add_argument("--context", required=True)
    solve_cmd.add_argument("--editable-file", default="src/book_filter.py")
    solve_cmd.add_argument("--target-tests", default="tests/test_target_feature.py")
    solve_cmd.add_argument("--regression-tests", default="tests/test_regression.py")
    solve_cmd.add_argument("--max-attempts", type=int, default=2)
    solve_cmd.set_defaults(func=_solve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
