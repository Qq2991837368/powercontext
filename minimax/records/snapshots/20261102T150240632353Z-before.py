"""Mini catalogue service: book listing filter.

The module is intentionally small so that the SDD workflow can show a complete
spec -> context -> code-change -> verification loop against a real database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["filter_books"]


def _is_number(value: Any) -> bool:
    """Return ``True`` for real numeric values (booleans are not prices)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_tag_list(value: Any) -> list[Any]:
    """Normalise a tags container into a plain list of tag values."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def filter_books(
    books: Iterable[Mapping[str, Any]],
    *,
    keyword: str | None = None,
    max_price: float | None = None,
    tags: Iterable[str] | None = None,
    include_out_of_stock: bool = False,
) -> list[dict[str, Any]]:
    """Filter catalogue books according to spec v1 (R1-R4).

    * R1: ``keyword`` matches ``title`` case-insensitively; ``None``/empty
      disables the filter.
    * R2: ``max_price`` keeps books with ``price <= max_price`` (inclusive);
      books without a numeric price are dropped when the filter is enabled.
    * R3: ``tags`` uses all-of semantics; books lacking a ``tags`` field are
      dropped when the filter is enabled.
    * R4: books explicitly marked ``in_stock=False`` are excluded unless
      ``include_out_of_stock=True`` is passed.

    The input objects are never mutated: every returned row is a shallow copy
    and the input ordering is preserved.
    """
    needle = (keyword or "").strip().lower()
    requested_tags = _as_tag_list(tags) if tags is not None else []
    filter_by_price = max_price is not None
    filter_by_tags = bool(requested_tags)

    selected: list[dict[str, Any]] = []
    for book in books:
        title = str(book.get("title", ""))
        if needle and needle not in title.lower():
            continue

        if filter_by_price:
            price = book.get("price")
            if not _is_number(price) or price > max_price:
                continue

        if filter_by_tags:
            if "tags" not in book:
                continue
            book_tags = _as_tag_list(book.get("tags"))
            if not all(tag in book_tags for tag in requested_tags):
                continue

        if not include_out_of_stock and book.get("in_stock", True) is False:
            continue

        selected.append(dict(book))
    return selected
