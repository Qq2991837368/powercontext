"""Close the SDD loop: MiniMax code change, pytest verification, state persistence."""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Paths
from .llm import MiniMaxClient
from .pytest_runner import run_tests
from .store import Store

SYSTEM_PROMPT = (
    "You are a careful software engineer. Follow the specification and tests in the supplied "
    "development context. Modify only the editable source file. Return the complete new file "
    "content between the <<<FILE ...>>> and <<<END>>> markers, with no commentary outside them."
)

MARKER_RE = re.compile(r"<<<FILE\s+([^>\n]+?)\s*>>>\s*(.*?)\s*<<<END>>>", re.DOTALL)
FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


class CandidateError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _rel(paths: Paths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def extract_candidate(text: str) -> tuple[str, str]:
    marker = MARKER_RE.search(text)
    if marker:
        declared = marker.group(1).strip()
        code = marker.group(2).strip()
        if code:
            return declared, code + "\n"
    fences = [block.strip() for block in FENCE_RE.findall(text)]
    usable = [block for block in fences if "def filter_books" in block]
    if usable:
        return "<fenced>", max(usable, key=len) + "\n"
    raise CandidateError("No <<<FILE ...>>> block or fenced Python code block found in the response")


def _validate_candidate(candidate: str, target_file: Path) -> None:
    if "def filter_books(" not in candidate:
        raise CandidateError("Candidate does not define filter_books")
    try:
        compile(candidate, str(target_file), "exec")
    except SyntaxError as exc:
        raise CandidateError(f"Candidate has a syntax error: {exc}") from exc


def _write_diff(paths: Paths, target_file: Path, before: str, after: str, path: Path) -> str:
    rel = _rel(paths, target_file)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="\n",
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diff, encoding="utf-8")
    return diff


def _session_payload(
    *,
    session_id: str,
    attempt: int,
    task: str,
    context: dict[str, Any],
    messages: list[dict[str, str]],
    response: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "attempt": attempt,
        "provider": "MiniMax",
        "endpoint": response.get("endpoint"),
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": task,
        "context_id": context.get("context_id"),
        "corpus_version": context.get("corpus_version"),
        "state_seq": (context.get("state") or {}).get("seq"),
        "request": {"messages": messages},
        "response": {
            "response_id": response.get("response_id"),
            "content": response.get("content"),
            "reasoning_content": response.get("reasoning_content"),
            "usage": response.get("usage"),
            "finish_reason": response.get("finish_reason"),
        },
    }


async def solve(
    store: Store,
    paths: Paths,
    *,
    task: str,
    context_path: Path,
    editable_file: str,
    target_tests: str,
    regression_tests: str,
    max_attempts: int = 2,
) -> dict[str, Any]:
    context = json.loads(context_path.read_text(encoding="utf-8"))
    manifest = await store.current_manifest()
    if manifest is None or int(context["corpus_version"]) != int(manifest["version"]):
        raise RuntimeError(
            "The context is stale: its corpus version does not match the stored corpus. "
            "Rebuild it with `python -m sdd_context context`."
        )
    target_file = (paths.demo / editable_file).resolve()
    if not target_file.is_file():
        raise RuntimeError(f"Editable file does not exist: {target_file}")

    stamp = _stamp()
    print(f"[solve] target file: {_rel(paths, target_file)}")
    before = run_tests(
        paths.demo,
        paths.test_runs,
        label="before-target",
        targets=[target_tests],
        stamp=stamp,
    )
    print(f"[solve] target tests before change: {before['summary']} (exit {before['exit_code']})")

    original = target_file.read_text(encoding="utf-8")
    client = MiniMaxClient()
    feedback = ""
    last_target: dict[str, Any] | None = None
    last_regression: dict[str, Any] | None = None
    last_session: dict[str, Any] | None = None

    for attempt in range(1, max_attempts + 1):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context["prompt"] + feedback},
        ]
        print(f"[solve] MiniMax attempt {attempt}/{max_attempts} ...")
        response = await client.complete(messages)
        session_id = f"{stamp}-attempt{attempt}"
        session = _session_payload(
            session_id=session_id,
            attempt=attempt,
            task=task,
            context=context,
            messages=messages,
            response=response,
            model=client.model,
        )
        session_path = paths.sessions / f"{session_id}.json"
        session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        last_session = session

        try:
            declared, candidate = extract_candidate(response.get("content") or "")
            _validate_candidate(candidate, target_file)
            if candidate.strip() == original.strip():
                raise CandidateError("Candidate is identical to the current file")
        except CandidateError as exc:
            print(f"[solve] rejected attempt {attempt}: {exc}")
            if response.get("finish_reason") == "length":
                reason = "The response was truncated by the output token limit. Return a shorter complete file."
            else:
                reason = str(exc)
            feedback = f"\n\n# Previous attempt was rejected\n{reason}\nReturn the complete corrected file."
            continue

        if Path(declared).name != target_file.name:
            print(f"[solve] warning: model declared {declared}, writing to {editable_file}")
        target_file.write_text(candidate, encoding="utf-8")
        last_target = run_tests(
            paths.demo,
            paths.test_runs,
            label=f"attempt{attempt}-target",
            targets=[target_tests],
            stamp=stamp,
        )
        last_regression = run_tests(
            paths.demo,
            paths.test_runs,
            label=f"attempt{attempt}-regression",
            targets=[regression_tests],
            stamp=stamp,
        )
        print(f"[solve] target: {last_target['summary']} | regression: {last_regression['summary']}")

        if last_target["ok"] and last_regression["ok"]:
            snapshot_before = paths.snapshots / f"{stamp}-before.py"
            snapshot_after = paths.snapshots / f"{stamp}-after.py"
            snapshot_before.write_text(original, encoding="utf-8")
            snapshot_after.write_text(candidate, encoding="utf-8")
            diff_path = paths.patches / f"{stamp}-{target_file.stem}.diff"
            diff = _write_diff(paths, target_file, original, candidate, diff_path)

            verification = {
                "before": {
                    "summary": before["summary"],
                    "exit_code": before["exit_code"],
                    "log": _rel(paths, before["log"]),
                },
                "attempt": attempt,
                "target": {
                    "summary": last_target["summary"],
                    "exit_code": last_target["exit_code"],
                    "log": _rel(paths, last_target["log"]),
                },
                "regression": {
                    "summary": last_regression["summary"],
                    "exit_code": last_regression["exit_code"],
                    "log": _rel(paths, last_regression["log"]),
                },
                "minimax_session": _rel(paths, session_path),
                "diff": _rel(paths, diff_path),
                "snapshots": [_rel(paths, snapshot_before), _rel(paths, snapshot_after)],
            }
            state = await store.record_state(
                task=task,
                status="done",
                summary=f"Implemented {editable_file} with MiniMax and verified target + regression tests.",
                verification=verification,
            )
            return {
                "status": "done",
                "stamp": stamp,
                "before": before["summary"],
                "target": last_target["summary"],
                "regression": last_regression["summary"],
                "diff": _rel(paths, diff_path),
                "diff_text": diff,
                "session": _rel(paths, session_path),
                "state": state,
            }

        target_file.write_text(original, encoding="utf-8")
        feedback = (
            "\n\n# Previous attempt failed the verification\n"
            f"Target tests: {last_target['summary']}\n"
            f"Regression tests: {last_regression['summary']}\n\n"
            "Target test output:\n"
            f"{last_target['output'][-4000:]}\n"
            "Return the complete corrected file."
        )

    verification = {
        "before": {"summary": before["summary"], "exit_code": before["exit_code"], "log": _rel(paths, before["log"])},
        "attempts": max_attempts,
        "target": None if last_target is None else last_target["summary"],
        "regression": None if last_regression is None else last_regression["summary"],
        "minimax_session": None if last_session is None else last_session["session_id"],
    }
    state = await store.record_state(
        task=task,
        status="failed",
        summary=f"MiniMax did not satisfy the target tests for {editable_file}.",
        verification=verification,
    )
    return {"status": "failed", "stamp": stamp, "before": before["summary"], "state": state}
