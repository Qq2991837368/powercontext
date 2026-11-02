"""Target tests for spec v1 requirements R2/R3/R4."""

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


def test_max_price_is_inclusive_and_excludes_missing_price():
    rows = filter_books(BOOKS, max_price=59.0, include_out_of_stock=True)
    assert ids(rows) == ["b1", "b2", "b4", "b6"]


def test_tags_require_all_requested_values():
    rows = filter_books(BOOKS, tags=["engineering", "classic"], include_out_of_stock=True)
    assert ids(rows) == ["b1", "b3"]


def test_out_of_stock_excluded_by_default():
    rows = filter_books(BOOKS)
    assert ids(rows) == ["b1", "b2", "b4", "b5", "b6"]


def test_include_out_of_stock_opt_in():
    rows = filter_books(BOOKS, include_out_of_stock=True)
    assert ids(rows) == ["b1", "b2", "b3", "b4", "b5", "b6"]


def test_combined_filters():
    rows = filter_books(
        BOOKS,
        keyword="clean",
        max_price=59.0,
        tags=["engineering"],
        include_out_of_stock=True,
    )
    assert ids(rows) == ["b1", "b2"]
