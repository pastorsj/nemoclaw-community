# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the Auto Ontology model for a pack's DuckDB database.

The model is derived, not authored: tables, columns and types come from DuckDB; keys come from the constraints in
schema.sql and, for prediction views, from the pack's `prediction.tables`; descriptions come from the pack's
ontology file. It is imported into Auto Ontology as-is (POST /api/model/import).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from demo_data.pack import Pack


@dataclass
class Table:
    name: str
    description: str
    kind: str  # "BASE TABLE" or "VIEW"
    primary_key: list[str]
    foreign_keys: list[tuple[str, str, str]]  # (column, referenced table, referenced column)
    columns: list[dict[str, Any]] = field(default_factory=list)


def build_model(pack: Pack, database: Path) -> dict[str, Any]:
    """Main tables get physical and semantic entries; prediction views are added to the physical layer only."""
    descriptions = yaml.safe_load(pack.path(pack.manifest["ontology"]).read_text(encoding="utf-8"))["tables"]
    with duckdb.connect(str(database), read_only=True) as connection:
        # Sample values are rendered as text; pin the zone so the model does not depend on the host's.
        connection.execute("SET TimeZone = 'UTC'")
        main = [_main_table(connection, name, descriptions) for name in pack.database_tables]
        for table in main:
            _add_columns(connection, "main", table, descriptions)
        main_layer = _schema_layer(pack.id, pack.database_name, "main", main)
        schemas, foreign_keys = [main_layer["schema"]], main_layer["foreign_keys"]
        if "prediction" in pack.manifest:
            prediction = pack.manifest["prediction"]
            views = [
                _prediction_view(name, spec, prediction["entity"], descriptions)
                for name, spec in prediction["tables"].items()
            ]
            for view in views:
                _add_columns(connection, prediction["schema"], view, descriptions)
            prediction_layer = _schema_layer(f"{pack.id}-prediction", pack.database_name, prediction["schema"], views)
            schemas.append(prediction_layer["schema"])
            foreign_keys += prediction_layer["foreign_keys"]
    return {
        "data_layer": {
            "databases": [{"id": f"database:{pack.id}", "dialect": "duckdb", "schemas": schemas}],
            "foreign_keys": foreign_keys,
            "joins": [],
        },
        "semantic_layer": {
            "terms": main_layer["terms"],
            "semantic_fks": main_layer["semantic_fks"],
            "sql_attributes": {"manual": [], "table": [], "sql": [], "bridge_table": []},
            "custom_analyses": [],
        },
        "zones": [],
    }


def _main_table(connection: duckdb.DuckDBPyConnection, name: str, descriptions: dict[str, Any]) -> Table:
    constraints = connection.execute(
        """
        SELECT constraint_type, constraint_column_names, referenced_table, referenced_column_names
        FROM duckdb_constraints()
        WHERE schema_name = 'main' AND table_name = ? AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
        ORDER BY constraint_index
        """,
        [name],
    ).fetchall()
    primary_key = next((list(columns) for kind, columns, _, _ in constraints if kind == "PRIMARY KEY"), [])
    foreign_keys = [
        (columns[0], table, referenced[0]) for kind, columns, table, referenced in constraints if kind == "FOREIGN KEY"
    ]
    return Table(name, _description(descriptions, name), "BASE TABLE", primary_key, foreign_keys)


def _prediction_view(name: str, spec: dict[str, Any], entity: dict[str, str], descriptions: dict[str, Any]) -> Table:
    link = spec.get("links_to_entity")
    foreign_keys = [(link, entity["table"], entity["key"])] if link else []
    return Table(name, _description(descriptions, name), "VIEW", [spec["primary_key"]], foreign_keys)


def _description(descriptions: dict[str, Any], table: str) -> str:
    return descriptions.get(table, {}).get("description") or table.replace("_", " ").capitalize()


def _add_columns(
    connection: duckdb.DuckDBPyConnection, schema: str, table: Table, descriptions: dict[str, Any]
) -> None:
    """Columns with their type, nullability, description and up to three sample values."""
    column_descriptions = descriptions.get(table.name, {}).get("columns", {})
    for _, name, data_type, not_null, _, _ in connection.execute(
        f"PRAGMA table_info('{schema}.{table.name}')"
    ).fetchall():
        samples = connection.execute(
            f'SELECT DISTINCT "{name}" FROM "{schema}"."{table.name}" WHERE "{name}" IS NOT NULL ORDER BY 1 LIMIT 3'
        ).fetchall()
        table.columns.append(
            {
                "name": name,
                "type": data_type,
                "nullable": not not_null,
                "description": column_descriptions.get(name, name.replace("_", " ").capitalize()),
                "sample_values": [str(value) for (value,) in samples],
            }
        )


def _schema_layer(model_id: str, database_name: str, schema: str, tables: list[Table]) -> dict[str, Any]:
    """One schema's physical tables and keys, plus a semantic term per table."""
    model_tables, foreign_keys, terms, semantic_fks = [], [], [], []
    for table in tables:
        table_id = f"table:{model_id}:{table.name}"
        columns, attributes = [], []
        for column in table.columns:
            column_id = f"column:{model_id}:{table.name}:{column['name']}"
            columns.append(
                {
                    "id": column_id,
                    "name": column["name"],
                    "description": column["description"],
                    "type": column["type"],
                    "sample_values": column["sample_values"],
                    "is_nullable": column["nullable"],
                    "is_unique": column["name"] in table.primary_key,
                }
            )
            attributes.append(
                {
                    "id": f"attribute:{model_id}:{table.name}:{column['name']}",
                    "name": column["name"].replace("_", " ").title(),
                    "description": column["description"],
                    "column_id": column_id,
                }
            )
        model_tables.append(
            {
                "id": table_id,
                "name": table.name,
                "description": table.description,
                "pk": table.primary_key,
                "type": table.kind,
                "columns": columns,
            }
        )
        terms.append(
            {
                "id": f"term:{model_id}:{table.name}",
                "name": table.name.replace("_", " ").title(),
                "description": table.description,
                "represents": [table_id],
                "columns_attributes": attributes,
            }
        )
        for column, referenced_table, referenced_column in table.foreign_keys:
            source_id = f"column:{model_id}:{table.name}:{column}"
            foreign_keys.append(
                {
                    "source_column_id": source_id,
                    "target_column_id": f"column:{model_id}:{referenced_table}:{referenced_column}",
                }
            )
            semantic_fks.append(
                {
                    "column_attribute_id": f"attribute:{model_id}:{referenced_table}:{referenced_column}",
                    "column_id": source_id,
                }
            )
    return {
        "schema": {
            "id": f"schema:{model_id}:{schema}",
            "name": schema,
            "database_name": database_name,
            "tables": model_tables,
        },
        "foreign_keys": foreign_keys,
        "terms": terms,
        "semantic_fks": semantic_fks,
    }
