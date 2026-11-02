"""PaperLens 的路径与环境配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT: Path = Path(__file__).resolve().parent.parent
CORPUS_DIR: Path = PACKAGE_ROOT / "corpus"
PAPERS_DIR: Path = CORPUS_DIR / "papers"
RECORDS_DIR: Path = PACKAGE_ROOT / "records"
SITE_DIR: Path = PACKAGE_ROOT / "site"
DEMO_QUESTIONS: Path = CORPUS_DIR / "demo-questions.json"
STATE_DIR_ENV = "PAPERLENS_STATE_DIR"
SCOPE_KEY_ENV = "PAPERLENS_SCOPE_KEY"
DEFAULT_SCOPE_KEY = "paperlens-demo-v1"
SCOPE_TITLE = "PaperLens 科研文献证据库"
SCOPE_SUMMARY = "ANN/向量检索方向的文献证据与研究记忆"


@dataclass(frozen=True)
class Paths:
    """PaperLens 运行期使用的目录集合。"""

    root: Path
    papers: Path
    records: Path
    site: Path
    state: Path
    db: Path
    evidence: Path

    @staticmethod
    def default() -> "Paths":
        """根据环境变量构造默认路径集合。"""
        raw_state = os.environ.get(STATE_DIR_ENV, "").strip()
        state = Path(raw_state).expanduser() if raw_state else RECORDS_DIR / "state"
        return Paths(
            root=PACKAGE_ROOT,
            papers=PAPERS_DIR,
            records=RECORDS_DIR,
            site=SITE_DIR,
            state=state,
            db=state / "seekdb",
            evidence=RECORDS_DIR / "evidence",
        )

    def ensure(self) -> None:
        """创建全部必需目录（幂等）。"""
        for directory in (self.papers, self.records, self.site, self.state, self.db, self.evidence):
            directory.mkdir(parents=True, exist_ok=True)


def load_paths() -> Paths:
    """加载当前环境的路径配置。"""
    return Paths.default()


def scope_key() -> str:
    """返回 scope 的幂等键，可由环境变量覆盖。"""
    raw = os.environ.get(SCOPE_KEY_ENV, "").strip()
    return raw or DEFAULT_SCOPE_KEY
