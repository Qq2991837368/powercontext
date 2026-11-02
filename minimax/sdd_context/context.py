"""Build a development context pack from seekdb retrieval plus PowerContext state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import Paths
from .store import Store


def _new_context_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _body(text: str) -> str:
    _, _, body = text.partition("\n")
    return body.rstrip()


def _state_block(state: dict[str, Any] | None, history: list[dict[str, Any]]) -> str:
    if state is None:
        return "No task state is stored yet (empty state)."
    lines = [
        f"- seq: {state.get('seq')}",
        f"- status: {state.get('status')}",
        f"- summary: {state.get('summary')}",
        f"- artifact: {state.get('artifact_id')}",
        f"- created_at: {state.get('created_at')}",
    ]
    if state.get("verification"):
        lines.append(f"- verification: {json.dumps(state['verification'], ensure_ascii=False)}")
    if state.get("notes"):
        lines.append(f"- notes: {state['notes']}")
    if history[1:]:
        lines.append("\nEarlier states:")
        for item in history[1:]:
            lines.append(
                f"- seq {item.get('seq')} [{item.get('status')}] {item.get('summary')}"
                f" (artifact {item.get('artifact_id')})"
            )
    return "\n".join(lines)


def render_prompt(context: dict[str, Any]) -> str:
    parts: list[str] = [
        "# Role",
        "You are the development assistant in a spec-driven workflow. The specification, code,",
        "and tests below were retrieved from the project database and must drive your change.",
        "",
        "# Business task",
        context["task"],
        "",
        "# Hard rules",
        f"- Modify only `{context['editable_file']}`.",
        "- Do not modify tests, specifications, or expected results.",
        "- Keep existing public behaviour compatible with the regression tests.",
        "- Return the complete new file content, not a diff.",
        "",
        "# Current task state (PowerContext)",
        f"scope: {context['scope_id']}",
        _state_block(context.get("state"), context.get("state_history", [])),
        "",
        "# Working file (exact revision read from seekdb)",
        f"### {context['working_file']['source']} "
        f"(artifact {context['working_file']['artifact_id']}, revision {context['working_file']['revision']})",
        _body(context["working_file"]["text"]),
        "",
        f"# Engineering context (seekdb corpus v{context['corpus_version']}, hybrid retrieval)",
    ]
    for section, title in (("spec", "Specifications"), ("code", "Source code"), ("test", "Tests")):
        parts.extend(["", f"## {title}"])
        hits = [
            hit
            for hit in context["retrieval"]["hits"].get(section, [])
            if hit["source"] != context["working_file"]["source"]
        ]
        if not hits:
            parts.append("(no additional hits)")
        for hit in hits:
            parts.append(
                f"### {hit['source']} "
                f"(artifact {hit['artifact_id']}, score {hit['score']}, "
                f"matched_by {', '.join(hit['matched_by']) or 'none'})"
            )
            parts.append(_body(hit["text"]))
    parts.extend(
        [
            "",
            "# Output format",
            f"<<<FILE {context['editable_file']}>>>",
            "<complete new file content>",
            "<<<END>>>",
            "",
            "Return only the marker block. No commentary outside the markers.",
        ]
    )
    return "\n".join(parts)


async def build_context(
    store: Store,
    paths: Paths,
    *,
    task: str,
    editable_file: str,
    limit_per_kind: int = 3,
) -> dict[str, Any]:
    manifest = await store.current_manifest()
    if manifest is None:
        raise RuntimeError("The corpus is empty. Run `python -m sdd_context init` first.")
    state = await store.latest_state()
    history = await store.state_history(limit=3)

    entry = next((item for item in manifest["files"] if item["path"] == editable_file), None)
    if entry is None:
        raise RuntimeError(f"{editable_file} is not part of the imported corpus; re-run init.")
    working_file = await store.get_artifact(entry["artifact_id"])

    hits: dict[str, list[dict[str, Any]]] = {}
    for kind in ("spec", "code", "test"):
        hits[kind] = await store.search(task, kind=kind, limit=limit_per_kind)

    context: dict[str, Any] = {
        "context_id": _new_context_id(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope_id": store.scope_id,
        "task": task,
        "editable_file": editable_file,
        "corpus_version": int(manifest["version"]),
        "corpus_files": [
            {"path": item["path"], "kind": item["kind"], "sha256": item["sha256"][:16]}
            for item in manifest["files"]
        ],
        "state": state,
        "state_history": history,
        "working_file": working_file,
        "retrieval": {"mode": "hybrid", "limit_per_kind": limit_per_kind, "hits": hits},
    }
    context["prompt"] = render_prompt(context)

    paths.contexts.mkdir(parents=True, exist_ok=True)
    stem = f"context-{context['context_id']}"
    json_path = paths.contexts / f"{stem}.json"
    md_path = paths.contexts / f"{stem}.md"
    json_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(context["prompt"] + "\n", encoding="utf-8")
    context["files"] = {
        "json": str(json_path.relative_to(paths.repo_root)),
        "prompt": str(md_path.relative_to(paths.repo_root)),
    }
    return context
