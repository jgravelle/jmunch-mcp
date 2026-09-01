"""Tabular backend: in-memory SQLite over a list of homogeneous dicts.

Inspired by jDataMunch's SQLite store but self-contained. Phase 2 will swap
this for jDataMunch proper once its programmatic-ingest API lands (PRD §10).

Column types are inferred by sampling the first N rows. Unknown/mixed types
fall back to TEXT. Everything stays in memory — no spill to disk for M1.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..errors import INVALID_ARGS, NOT_APPLICABLE, make_error

# Column-type inference
_SAMPLE_ROWS = 100

# jMRI-style stable-ish id within a tabular handle: "row:<index>"
# (not cross-session stable — handles are session-scoped by PRD §4)


class TabularBackend:
    kind = "tabular"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._row_count = len(rows)
        self._columns, self._col_types = _infer_columns(rows)
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._load(rows)
        self._size_bytes = sum(len(json.dumps(r, default=str)) for r in rows)

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    def _load(self, rows: list[dict[str, Any]]) -> None:
        cols_sql = ", ".join(f'"{c}" {self._col_types[c]}' for c in self._columns)
        self._conn.execute(f'CREATE TABLE t ({cols_sql})')
        placeholders = ", ".join("?" for _ in self._columns)
        insert_sql = f'INSERT INTO t VALUES ({placeholders})'
        batch = [
            tuple(_coerce(row.get(c), self._col_types[c]) for c in self._columns)
            for row in rows
        ]
        self._conn.executemany(insert_sql, batch)
        self._conn.commit()

    def summary(self) -> dict[str, Any]:
        return {
            "row_count": self._row_count,
            "column_count": len(self._columns),
            "columns": [
                {"name": c, "type": self._col_types[c]} for c in self._columns
            ],
            "sample": self._sample_rows(3),
        }

    def describe(self) -> dict[str, Any]:
        stats = {}
        for c in self._columns:
            t = self._col_types[c]
            col_stat: dict[str, Any] = {"type": t}
            if t in ("INTEGER", "REAL"):
                row = self._conn.execute(
                    f'SELECT MIN("{c}") AS mn, MAX("{c}") AS mx, AVG("{c}") AS av FROM t'
                ).fetchone()
                col_stat.update(min=row["mn"], max=row["mx"], avg=row["av"])
            null_ct = self._conn.execute(
                f'SELECT COUNT(*) AS n FROM t WHERE "{c}" IS NULL'
            ).fetchone()["n"]
            col_stat["null_count"] = null_ct
            stats[c] = col_stat
        return {
            "row_count": self._row_count,
            "column_count": len(self._columns),
            "columns": [
                {"name": c, **stats[c]} for c in self._columns
            ],
        }

    def peek(self, n: int, *, where: str = "head") -> Any:
        n = max(1, min(n, 1000))
        if where == "tail":
            sql = (
                f'SELECT * FROM t WHERE rowid > (SELECT MAX(rowid) FROM t) - {n} '
                'ORDER BY rowid'
            )
        else:
            sql = f'SELECT * FROM t LIMIT {n}'
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def slice(self, selector: str, *, max_rows: int = 100) -> Any:
        """`selector` is a SQL WHERE clause (no leading "WHERE")."""
        if not _is_safe_where(selector):
            return make_error(INVALID_ARGS, "Unsafe or malformed WHERE clause.", selector=selector)
        max_rows = max(1, min(max_rows, 1000))
        try:
            rows = self._conn.execute(
                f'SELECT * FROM t WHERE {selector} LIMIT {max_rows}'
            ).fetchall()
        except sqlite3.Error as e:
            return make_error(INVALID_ARGS, f"SQL error: {e}", selector=selector)
        return [dict(r) for r in rows]

    def search(self, query: str, *, max_results: int = 10) -> Any:
        """Naive lexical substring search across TEXT columns."""
        text_cols = [c for c, t in self._col_types.items() if t == "TEXT"]
        if not text_cols:
            return make_error(
                NOT_APPLICABLE, "No TEXT columns available for search.", columns=list(self._columns)
            )
        like = f"%{query}%"
        where = " OR ".join(f'"{c}" LIKE ?' for c in text_cols)
        max_results = max(1, min(max_results, 100))
        rows = self._conn.execute(
            f'SELECT * FROM t WHERE {where} LIMIT ?',
            (*([like] * len(text_cols)), max_results),
        ).fetchall()
        return [dict(r) for r in rows]

    def aggregate(
        self, op: str, field: str | None = None, *, group_by: str | None = None
    ) -> Any:
        op_u = op.upper()
        if op_u not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
            return make_error(INVALID_ARGS, f"Unsupported op '{op}'.", allowed=["count", "sum", "avg", "min", "max"])
        if op_u != "COUNT" and field is None:
            return make_error(INVALID_ARGS, f"Op '{op}' requires a field.")
        if field is not None and field not in self._columns:
            return make_error(INVALID_ARGS, f"Unknown field '{field}'.", columns=list(self._columns))
        if group_by is not None and group_by not in self._columns:
            return make_error(INVALID_ARGS, f"Unknown group_by '{group_by}'.", columns=list(self._columns))

        expr = "COUNT(*)" if op_u == "COUNT" and field is None else f'{op_u}("{field}")'

        if group_by:
            rows = self._conn.execute(
                f'SELECT "{group_by}" AS grp, {expr} AS value FROM t GROUP BY "{group_by}" ORDER BY value DESC'
            ).fetchall()
            return [{"group": r["grp"], "value": r["value"]} for r in rows]

        value = self._conn.execute(f'SELECT {expr} AS value FROM t').fetchone()["value"]
        return {"op": op.lower(), "field": field, "value": value}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _sample_rows(self, n: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(f'SELECT * FROM t LIMIT {n}').fetchall()
        return [dict(r) for r in rows]


def _infer_columns(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    seen: list[str] = []
    keyset: set[str] = set()
    for row in rows[:_SAMPLE_ROWS]:
        for k in row:
            if k not in keyset:
                seen.append(k)
                keyset.add(k)

    types: dict[str, str] = {}
    for col in seen:
        # Track only what WIDENS the type. INTEGER is the floor, so an int (or a
        # bool, which is an int subclass) needs no flag of its own.
        t_real = t_text = False
        any_non_null = False
        for row in rows[:_SAMPLE_ROWS]:
            v = row.get(col)
            if v is None:
                continue
            any_non_null = True
            if isinstance(v, (bool, int)):
                continue
            if isinstance(v, float):
                t_real = True
            else:
                t_text = True
                break
        if not any_non_null or t_text:
            types[col] = "TEXT"
        elif t_real:
            types[col] = "REAL"
        else:
            types[col] = "INTEGER"
    return seen, types


def _coerce(value: Any, sqlite_type: str) -> Any:
    if value is None:
        return None
    if sqlite_type in ("INTEGER", "REAL"):
        return value if isinstance(value, (int, float, bool)) else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value if isinstance(value, str) else str(value)


# A conservative WHERE-clause guardrail. We don't try to be a full SQL parser
# here — we just reject anything that could smuggle a second statement or
# touch other tables. SQLite in-memory with a single table limits the blast
# radius, but layered defense is free.
_UNSAFE = re.compile(
    r"(;|--|/\*|\*/|\battach\b|\bpragma\b|\bdrop\b|\bdelete\b|\binsert\b|\bupdate\b|\balter\b|\bcreate\b)",
    re.IGNORECASE,
)


def _is_safe_where(clause: str) -> bool:
    if not clause or len(clause) > 2000:
        return False
    return _UNSAFE.search(clause) is None
