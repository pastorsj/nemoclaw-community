# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run one data-viewer SQL query in a separate, resource-limited Python process.

``query.py`` starts ``python -I worker.py`` and writes ``{path, database_name, sql, tables, max_rows}``
to its stdin.
This file imports nothing from the API, so it runs in isolated mode. The query must be a single
SELECT over the database's own tables and views; each table is replaced by a projection of its
known columns before DuckDB sees the query. DuckDB opens the file read-only, with external
access, extensions and spilling off.
"""

from __future__ import annotations

import json
import math
import resource
import sys
import tempfile
from typing import Any

import duckdb
from sqlglot import exp
from sqlglot import parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

MAX_ROWS = 100
MAX_COLUMNS = 64
MAX_RESPONSE_BYTES = 1_048_576
_UNSUPPORTED = "This query uses an unsupported SQL operation or function."


class QueryRejected(ValueError):
    """A query the viewer refuses; its message is written here and safe to show."""


def validate_sql(sql: str, tables: dict[str, list[str]], database_name: str) -> str:
    """Return the query rewritten to read only known columns of known tables, or raise QueryRejected."""
    try:
        statements = parse(sql, read="duckdb")
    except ParseError as error:
        raise QueryRejected("The query could not be parsed. Check its SQL syntax.") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select | exp.SetOperation):
        raise QueryRejected("Only one SELECT query is supported.")
    tree = statements[0]
    if any(isinstance(node, exp.Command | exp.Into | exp.Lock | exp.DML | exp.DDL) for node in tree.walk()):
        raise QueryRejected(_UNSUPPORTED)
    _bind_functions(tree)
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if not isinstance(source, exp.Table):
                continue  # CTEs and subqueries are traversed as scopes of their own
            if not isinstance(source.this, exp.Identifier) or (
                source.catalog and source.catalog.casefold() != database_name.casefold()
            ):
                raise QueryRejected("Only the database's own tables and views are supported.")
            columns = tables.get(f"{source.db or 'main'}.{source.name}".casefold())
            if not columns:
                raise QueryRejected("A table is not part of this database.")
            for column in scope.columns:
                if column.table.casefold() == source.name.casefold() and column.db:
                    column.set("db", None)
                    column.set("catalog", None)
            # Read only the known columns, which also makes SELECT * mean the published schema.
            projection = exp.select(*(exp.column(name, quoted=True) for name in columns)).from_(source.copy())
            source.replace(projection.subquery(alias=source.alias or source.name))
    for node in tree.find_all(exp.From, exp.Join):
        if not isinstance(node.this, exp.Table | exp.Subquery):
            raise QueryRejected("Table functions are not supported.")
    return tree.sql(dialect="duckdb")


def _bind_functions(tree: exp.Expression) -> None:
    """Reject unknown functions (user macros can shadow built-ins); bind timezone() to the system one."""
    for node in list(tree.find_all(exp.Anonymous)):
        if node.name.casefold() != "timezone" or isinstance(node.parent, exp.Dot) or len(node.expressions) != 2:
            raise QueryRejected(_UNSUPPORTED)
        node.replace(_system_timezone(node.expressions[0], node.expressions[1]))
    for node in list(tree.find_all(exp.AtTimeZone)):
        node.replace(_system_timezone(node.args["zone"], node.this))


def _system_timezone(zone: exp.Expression, value: exp.Expression) -> exp.Expression:
    function = exp.Anonymous(this="timezone", expressions=[zone.copy(), value.copy()])
    namespace = exp.Dot(this=exp.to_identifier("system"), expression=exp.to_identifier("main"))
    return exp.Dot(this=namespace, expression=function)


def display_cell(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 - 1 else str(value)  # beyond this JavaScript loses precision
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return f"[binary: {len(value)} bytes]"
    return str(value)[:512]


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    sql = validate_sql(payload["sql"], payload["tables"], payload["database_name"])
    with (
        tempfile.TemporaryDirectory(prefix="demo-api-query-") as temp_directory,
        duckdb.connect(
            payload["path"],
            read_only=True,
            config={
                "enable_external_access": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "threads": "1",
                "memory_limit": "256MB",
                "temp_directory": temp_directory,
                "max_temp_directory_size": "0B",
            },
        ) as connection,
    ):
        statements = connection.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise QueryRejected("Only one SELECT query is supported.")
        max_rows = min(int(payload.get("max_rows", MAX_ROWS)), MAX_ROWS)
        result = connection.execute(f"SELECT * FROM ({sql}) AS viewer_result LIMIT {max_rows + 1}")
        if len(result.description) > MAX_COLUMNS:
            raise QueryRejected(f"Select at most {MAX_COLUMNS} columns.")
        rows = result.fetchall()
        response = {
            "columns": [str(item[0])[:256] for item in result.description],
            "types": [str(item[1])[:64] for item in result.description],
            "rows": [[display_cell(cell) for cell in row] for row in rows[:max_rows]],
            "truncated": len(rows) > max_rows,
        }
        if len(json.dumps(response).encode()) > MAX_RESPONSE_BYTES:
            raise QueryRejected("The result is too large. Select fewer columns or shorter values.")
        return response


if __name__ == "__main__":
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        print(json.dumps(execute(json.load(sys.stdin))))
    except QueryRejected as error:
        # Only messages this file wrote may leave the worker.
        print(json.dumps({"error": "query_rejected", "message": str(error)}))
    except Exception:  # noqa: BLE001 - DuckDB errors can quote data; report only that the query failed
        print(json.dumps({"error": "query_failed"}))
