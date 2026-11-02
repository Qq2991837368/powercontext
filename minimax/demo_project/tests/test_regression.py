"""Regression tests for spec v1: behaviour that must keep working."""

from src.book_filter import filter_books

STOCKED = [
    {"id": "b1", "title": "Clean Code", "price": 45.0, "tags": ["engineering"], "in_stock": True},
    {"id": "b2", "title": "Clean Architecture", "price": 59.0, "tags": ["engineering"], "in_stock": True},
    {"id": "b4", "title": "Dune", "price": 30.0, "tags": ["fiction"], "in_stock": True},
    {"id": "b6", "title": "Legacy Notes", "price": 12.0, "tags": ["reference"], "promoted": True},
]


def ids(rows):
    return [row["id"] for row in rows]


def test_keyword_is_case_insensitive():
    rows = filter_books(STOCKED, keyword="CLEAN")
    assert ids(rows) == ["b1", "b2"]


def test_keyword_absent_returns_all():
    rows = filter_books(STOCKED, keyword=None)
    assert ids(rows) == ["b1", "b2", "b4", "b6"]


def test_keyword_without_match_returns_empty():
    rows = filter_books(STOCKED, keyword="nonexistent")
    assert rows == []


def test_input_is_not_mutated_and_order_is_preserved():
    rows = filter_books(STOCKED, keyword="")
    assert ids(rows) == ["b1", "b2", "b4", "b6"]
    assert ids(STOCKED) == ["b1", "b2", "b4", "b6"]

    rows[0]["title"] = "mutated copy"
    assert STOCKED[0]["title"] == "Clean Code"


def test_extra_fields_are_preserved():
    rows = filter_books(STOCKED, keyword="legacy")
    assert rows == [STOCKED[3]]
    assert rows[0]["promoted"] is True
