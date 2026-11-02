"""Run pytest and persist the raw output as verification evidence."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PASSED_RE = re.compile(r"(\d+) passed")
FAILED_RE = re.compile(r"(\d+) failed")
ERROR_RE = re.compile(r"(\d+) error")


def run_tests(demo_dir: Path, records_dir: Path, *, label: str, targets: list[str], stamp: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", *targets, "-q", "--color=no", "-p", "no:cacheprovider"]
    started = datetime.now(timezone.utc)
    completed = subprocess.run(
        command,
        cwd=demo_dir,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = (
        f"# command: {' '.join(command)}\n"
        f"# cwd: {demo_dir}\n"
        f"# started_at: {started.isoformat(timespec='seconds')}\n"
        f"# exit_code: {completed.returncode}\n\n"
        f"{completed.stdout}\n{completed.stderr}"
    ).rstrip() + "\n"

    records_dir.mkdir(parents=True, exist_ok=True)
    log_path = records_dir / f"{stamp}-{label}.log"
    log_path.write_text(output, encoding="utf-8")

    passed = int(match.group(1)) if (match := PASSED_RE.search(output)) else 0
    failed = int(match.group(1)) if (match := FAILED_RE.search(output)) else 0
    errors = int(match.group(1)) if (match := ERROR_RE.search(output)) else 0
    return {
        "label": label,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "ok": completed.returncode == 0,
        "summary": f"{passed} passed, {failed} failed, {errors} errors",
        "log": log_path,
        "output": output,
    }
