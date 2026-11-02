"""Target tests for spec v2 requirement R5 (min_price)."""

from src.book_filter import filter_books

BOOKS = [
    {"id": "b1", "title": "Clean Code", "price": 45.0, "tags": ["engineering", "classic"], "in_stock": True},
    {"id": "b2", "title": "Clean Architecture", "price": 59.0, "tags": ["engineering"], "in_stock": True},
    {"id": "b3", "title": "Refactoring", "price": 60.0, "tags": ["engineering", "classic"], "in_stock": False},
    {"id": "b4", "title": "Dune", "price": 30.0, "tags": ["fiction"], "in_stock": True},
    {"id": "b5", "title": "Untitled Draft", "tags": ["engineering"], "in_stock": True},
    {"id": "b6", "title": "Legacy Notes", "price": 12.0, "tags": ["reference"], "promoted": True},
]


def ids(rows):
    return [row["id"] for row in rows]


def test_min_price_is_inclusive_and_excludes_missing_price():
    rows = filter_books(BOOKS, min_price=45.0, include_out_of_stock=True)
    assert ids(rows) == ["b1", "b2", "b3"]


def test_price_range_is_a_closed_interval():
    rows = filter_books(BOOKS, min_price=45.0, max_price=59.0, include_out_of_stock=True)
    assert ids(rows) == ["b1", "b2"]


def test_empty_price_range_returns_no_rows():
    rows = filter_books(BOOKS, min_price=61.0, max_price=70.0, include_out_of_stock=True)
    assert rows == []
