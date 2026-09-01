from jmunch_mcp.backends.tabular import TabularBackend


ROWS = [
    {"id": 1, "state": "open", "title": "auth bug", "upvotes": 3},
    {"id": 2, "state": "closed", "title": "typo in docs", "upvotes": 0},
    {"id": 3, "state": "open", "title": "auth crash", "upvotes": 7},
    {"id": 4, "state": "open", "title": "slow query", "upvotes": 1},
    {"id": 5, "state": "closed", "title": "fix build", "upvotes": 2},
]


def test_summary_and_describe():
    b = TabularBackend(ROWS)
    s = b.summary()
    assert s["row_count"] == 5
    assert s["column_count"] == 4
    d = b.describe()
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["upvotes"]["type"] == "INTEGER"
    assert cols["upvotes"]["min"] == 0
    assert cols["upvotes"]["max"] == 7


def test_peek_head_and_tail():
    b = TabularBackend(ROWS)
    head = b.peek(2)
    assert [r["id"] for r in head] == [1, 2]
    tail = b.peek(2, where="tail")
    assert [r["id"] for r in tail] == [4, 5]


def test_slice_where_clause():
    b = TabularBackend(ROWS)
    open_rows = b.slice("state = 'open'")
    assert [r["id"] for r in open_rows] == [1, 3, 4]


def test_slice_rejects_injection():
    b = TabularBackend(ROWS)
    result = b.slice("1=1; DROP TABLE t")
    assert isinstance(result, dict) and result.get("code") == "INVALID_ARGS"


def test_aggregate_count_and_group():
    b = TabularBackend(ROWS)
    total = b.aggregate("count")
    assert total["value"] == 5
    by_state = b.aggregate("count", group_by="state")
    as_map = {g["group"]: g["value"] for g in by_state}
    assert as_map == {"open": 3, "closed": 2}


def test_aggregate_sum():
    b = TabularBackend(ROWS)
    result = b.aggregate("sum", field="upvotes")
    assert result["value"] == 13


def test_aggregate_rejects_unknown_field():
    b = TabularBackend(ROWS)
    result = b.aggregate("sum", field="nonexistent")
    assert result.get("code") == "INVALID_ARGS"


def test_search_substring():
    b = TabularBackend(ROWS)
    hits = b.search("auth")
    assert {r["id"] for r in hits} == {1, 3}


def test_column_type_inference_covers_every_branch():
    """Pins `_infer_columns` across all four outcomes.

    Written before refactoring away a vestigial `t_int` flag, so the refactor
    had something to be checked against. bool is a subclass of int, which is
    the case most likely to break under a rewrite: True must widen to INTEGER,
    never to TEXT.
    """
    from jmunch_mcp.backends.tabular import _infer_columns

    _, types = _infer_columns([
        {"i": 1, "f": 1.5, "s": "x", "b": True, "mixed": 1, "nulls": None},
        {"i": 2, "f": 2.0, "s": "y", "b": False, "mixed": "now text", "nulls": None},
    ])

    assert types["i"] == "INTEGER"
    assert types["f"] == "REAL"
    assert types["s"] == "TEXT"
    assert types["b"] == "INTEGER", "bool is an int subclass and must not read as TEXT"
    assert types["mixed"] == "TEXT", "any text in the sample widens the column to TEXT"
    assert types["nulls"] == "TEXT", "an all-null column defaults to TEXT"


def test_int_then_float_widens_to_real():
    from jmunch_mcp.backends.tabular import _infer_columns

    _, types = _infer_columns([{"n": 1}, {"n": 2.5}])
    assert types["n"] == "REAL"
