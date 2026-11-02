"""Shared paths and settings for the SDD context module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPE_KEY = "minimax-sdd-demo"
SCOPE_TITLE = "MiniMax SDD demo"
SCOPE_SUMMARY = "Spec-driven development context for the book-filter mini project"


@dataclass(frozen=True)
class Paths:
    root: Path
    demo: Path
    state: Path
    records: Path

    @property
    def db(self) -> Path:
        return self.state / "seekdb"

    @property
    def backups(self) -> Path:
        return self.state / "backups"

    @property
    def contexts(self) -> Path:
        return self.records / "contexts"

    @property
    def sessions(self) -> Path:
        return self.records / "minimax-sessions"

    @property
    def test_runs(self) -> Path:
        return self.records / "test-runs"

    @property
    def patches(self) -> Path:
        return self.records / "patches"

    @property
    def snapshots(self) -> Path:
        return self.records / "snapshots"

    @property
    def repo_root(self) -> Path:
        return self.root.parent

    def ensure(self) -> None:
        for path in (
            self.state,
            self.backups,
            self.records,
            self.contexts,
            self.sessions,
            self.test_runs,
            self.patches,
            self.snapshots,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_paths() -> Paths:
    root = Path(os.environ.get("SDD_ROOT", PACKAGE_ROOT)).resolve()
    demo = Path(os.environ.get("SDD_DEMO_DIR", root / "demo_project")).resolve()
    state = Path(os.environ.get("SDD_STATE_DIR", root / ".state")).resolve()
    records = Path(os.environ.get("SDD_RECORDS_DIR", root / "records")).resolve()
    return Paths(root=root, demo=demo, state=state, records=records)


def scope_key() -> str:
    return os.environ.get("SDD_SCOPE_KEY", DEFAULT_SCOPE_KEY)
