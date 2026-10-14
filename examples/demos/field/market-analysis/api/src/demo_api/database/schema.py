# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The structured source's physical schema, read from its DuckDB file (read-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


def qualified_name(schema: str, table: str) -> str:
    """How the viewer names a table: bare in ``main``, ``schema.table`` elsewhere."""
    return table if schema == "main" else f"{schema}.{table}"


def read_schema(path: Path) -> dict[str, Any]:
    """Tables and views with their columns, primary keys and foreign keys. Blocking; run it in a thread."""
    with duckdb.connect(str(path), read_only=True, config={"enable_external_access": "false"}) as connection:
        tables = connection.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database() ORDER BY table_schema, table_name"
        ).fetchall()
        columns = connection.execute(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable = 'YES' "
            "FROM information_schema.columns WHERE table_catalog = current_database() "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
        constraints = connection.execute(
            "SELECT schema_name, table_name, constraint_type, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() "
            "WHERE database_name = current_database() AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')"
        ).fetchall()

    primary_keys = {(s, t): list(cols) for s, t, kind, cols, _, _ in constraints if kind == "PRIMARY KEY"}
    result: list[dict[str, Any]] = []
    for schema, table, table_type in tables:
        keys = primary_keys.get((schema, table), [])
        result.append(
            {
                "name": qualified_name(schema, table),
                "schema": schema,
                "kind": "view" if table_type == "VIEW" else "table",
                "primary_key": keys,
                "columns": [
                    {"name": name, "type": data_type, "nullable": nullable, "primary_key": name in keys}
                    for s, t, name, data_type, nullable in columns
                    if (s, t) == (schema, table)
                ],
            }
        )
    relationships = [
        {
            "from_table": qualified_name(schema, table),
            "from_column": column,
            "to_table": qualified_name(schema, referenced),
            "to_column": referenced_column,
        }
        for schema, table, kind, cols, referenced, referenced_cols in constraints
        if kind == "FOREIGN KEY"
        for column, referenced_column in zip(cols, referenced_cols, strict=True)
    ]
    return {"tables": result, "relationships": relationships}


def governed_columns(schema: dict[str, Any]) -> dict[str, list[str]]:
    """``{"schema.table": [columns]}`` (lower-case keys), the only data a viewer query may read."""
    return {
        f"{table['schema']}.{table['name'].rsplit('.', 1)[-1]}".casefold(): [c["name"] for c in table["columns"]]
        for table in schema["tables"]
    }
