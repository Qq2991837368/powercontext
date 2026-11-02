"""Mini catalogue service: book listing filter.

The module is intentionally small so that the SDD workflow can show a complete
spec -> context -> code-change -> verification loop against a real database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def filter_books(
    books: Iterable[Mapping[str, Any]],
    *,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    """Return books whose title contains ``keyword`` (case-insensitive)."""
    needle = (keyword or "").strip().lower()
    selected: list[dict[str, Any]] = []
    for book in books:
        title = str(book.get("title", ""))
        if needle and needle not in title.lower():
            continue
        selected.append(dict(book))
    return selected
