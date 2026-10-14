# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the structured part of a pack: Parquet tables, the DuckDB database, the ontology and the prediction graph.

tables/<table>.parquet               every table, as the market importer wrote it
structured/<database_name>.duckdb    schema.sql, then the tables, then the rendered views
ontology/model.yaml                  Auto Ontology model
prediction/graph.json                prediction views, keys, time columns, anchor and population
prediction/templates.json            PQL templates
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from demo_data import external
from demo_data import market
from demo_data import ontology
from demo_data.pack import Pack
from demo_data.pack import applicable_contracts
from demo_data.pack import contract_errors
from demo_data.pack import sql_literal

# The readers (api, market-analytics, Auto Ontology) may lag behind this builder's DuckDB. Pinning the on-disk
# format (DuckDB 1.5's default today) stops a writer upgrade from silently changing it; these files open in
# DuckDB 1.1.3 and later. Still upgrade the readers before the writer.
STORAGE_VERSION = "v1.0.0"


class BuildError(Exception):
    """The structured build produced data that does not match the pack."""


def build(
    pack: Pack, profile: str, contracts: list[dict[str, Any]], out: Path, *, sources_dir: Path, cache_dir: Path
) -> dict[str, Any]:
    """Build every structured artifact under `out`; returns the part's receipt: row counts, and the import's."""
    tables_dir = out / "tables"
    imported = produce_tables(pack, profile, tables_dir, sources_dir, cache_dir)
    receipt = {"rows": check_tables(pack, profile, contracts, tables_dir), "import": imported}
    database = out / "structured" / f"{pack.database_name}.duckdb"
    load_database(pack, tables_dir, database)
    model = ontology.build_model(pack, database)
    write_text(out / "ontology" / "model.yaml", yaml.safe_dump(model, sort_keys=False, allow_unicode=True))
    if "prediction" in pack.manifest:
        write_prediction(pack, database, out / "prediction")
    return receipt


def produce_tables(pack: Pack, profile: str, tables_dir: Path, sources_dir: Path, cache_dir: Path) -> dict[str, Any]:
    """Import the tables from the pack's raw market dataset: an external one, or its generator's."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    dataset = market_dataset(pack, profile, sources_dir, cache_dir)
    imported = market.import_tables(pack, dataset, tables_dir, cache_dir)
    missing = [name for name in pack.tables if not (tables_dir / f"{name}.parquet").is_file()]
    if missing:
        raise BuildError(f"no table was written for {missing}")
    return imported


def market_dataset(pack: Pack, profile: str, sources_dir: Path, cache_dir: Path) -> external.Dataset:
    """The raw market dataset: an external one (verified by `fetch`), or the generator's, made once per input.

    A generator that feeds the market importer writes a raw dataset with its own manifest.json (the layout a real
    dataset uses). It is cached in <cache>/generated/<key>/, keyed by the pack's content and the profile.
    """
    dataset_id = pack.manifest["market"]["bars"]["dataset"]
    if pack.origins[dataset_id]["kind"] == "external":
        return external.datasets(pack.manifest, sources_dir)[dataset_id]
    root = market_root(pack, profile, sources_dir, cache_dir)
    if not (root / "manifest.json").is_file():
        staging = root.with_name(f".{root.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        generate(pack, profile, staging)
        staging.rename(root)
    files = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["files"]
    dataset = external.Dataset(dataset_id, root, "manifest.json", external.fingerprint(files), 0)
    external.verify(dataset)  # records the hashes once; the import then checks sizes and mtimes only
    return dataset


def market_root(pack: Pack, profile: str, sources_dir: Path, cache_dir: Path) -> Path:
    """Where the raw market dataset is: $DATA_SOURCE_DIR/<id>, or the generator's output in the cache."""
    dataset_id = pack.manifest["market"]["bars"]["dataset"]
    if pack.origins[dataset_id]["kind"] == "external":
        return sources_dir / dataset_id
    return cache_dir / "generated" / pack.digest(profile, [], "generated")[:16]


def generate(pack: Pack, profile: str, out: Path) -> None:
    """Run the pack's generator: `python <entrypoint> --profile <profile> --out <out>`."""
    entrypoint = pack.path(pack.manifest["generator"]["entrypoint"])
    command = [sys.executable, str(entrypoint), "--profile", profile, "--out", str(out)]
    subprocess.run(command, check=True, cwd=pack.directory)


def check_tables(pack: Pack, profile: str, contracts: list[dict[str, Any]], tables_dir: Path) -> dict[str, int]:
    """Check the Parquet tables against the pack's tool contracts and the profile's expected row counts."""
    with duckdb.connect() as connection:
        relations = {name: f"read_parquet({sql_literal(str(tables_dir / f'{name}.parquet'))})" for name in pack.tables}
        errors = [
            error
            for contract in applicable_contracts(pack, contracts)
            for error in contract_errors(contract, pack, connection, relations)
        ]
        rows = {
            name: connection.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
            for name, relation in relations.items()
        }
    expected = pack.profiles.get(profile, {}).get("expected_rows", {})
    errors += [
        f"{table} has {rows[table]:,} rows; profile {profile} expects {count:,}"
        for table, count in expected.items()
        if rows[table] != count
    ]
    if errors:
        raise BuildError("; ".join(errors))
    return rows


def load_database(pack: Pack, tables_dir: Path, database: Path) -> None:
    """schema.sql, then every database table from Parquet (columns matched by name), then the views."""
    database.parent.mkdir(parents=True, exist_ok=True)
    config = {"storage_compatibility_version": STORAGE_VERSION}
    with duckdb.connect(str(database), config=config) as connection:
        connection.execute(pack.path(pack.structured["schema"]).read_text(encoding="utf-8"))
        for table in pack.database_tables:
            connection.execute(
                f"INSERT INTO main.{table} BY NAME SELECT * FROM read_parquet(?)",
                [str(tables_dir / f"{table}.parquet")],
            )
        for view in pack.structured.get("views", []):
            connection.execute(render_view(pack, pack.path(view).read_text(encoding="utf-8")))
        connection.execute("CHECKPOINT")


def render_view(pack: Pack, sql: str) -> str:
    """Fill the {{placeholders}} of a view file from the prediction section."""
    values: dict[str, str] = {}
    if "prediction" in pack.manifest:
        prediction = pack.manifest["prediction"]
        anchor = datetime.fromisoformat(prediction["anchor"]).astimezone(UTC)
        values = {
            "anchor_date": anchor.date().isoformat(),
            "anchor_timestamp": anchor.isoformat(),
            "horizon_sessions": str(prediction["horizon_sessions"]),
        }
        if "ids" in prediction["population"]:
            values["population_literals"] = ", ".join(sql_literal(entity) for entity in prediction["population"]["ids"])
    for name, value in values.items():
        sql = sql.replace("{{" + name + "}}", value)
    if "{{" in sql:
        raise BuildError(f"unresolved placeholder in view SQL near {sql[sql.index('{{') :][:40]!r}")
    return sql


def write_prediction(pack: Pack, database: Path, out: Path) -> None:
    """The prediction graph as built (with each view's row count) and its PQL templates."""
    prediction = pack.manifest["prediction"]
    schema = prediction["schema"]
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = {
            name: connection.execute(f"SELECT count(*) FROM {schema}.{name}").fetchone()[0]
            for name in prediction["tables"]
        }
        population = [
            row[0]
            for row in connection.execute(f"SELECT * FROM {schema}.{prediction['population']['view']}").fetchall()
        ]
    # Without pinned ids, the population is whatever its view selects at the anchor (e.g. the 50 most liquid).
    ids = prediction["population"].get("ids", population)
    missing = sorted(set(ids) - set(population))
    if missing:
        raise BuildError(f"population ids are not entities at the anchor: {missing}")
    if not ids or len(ids) > 1000:
        raise BuildError(f"the population has {len(ids)} entities; Kumo needs 1 to 1,000")
    graph = {
        "schema": schema,
        "anchor": prediction["anchor"],
        "horizon_sessions": prediction["horizon_sessions"],
        "entity": prediction["entity"],
        "population": {"view": prediction["population"]["view"], "ids": ids},
        "tables": [
            {
                "name": name,
                "primary_key": table["primary_key"],
                "time_column": table.get("time_column"),
                "links_to_entity": table.get("links_to_entity"),
                "rows": rows[name],
            }
            for name, table in prediction["tables"].items()
        ],
        "forbidden_tables": prediction.get("forbidden_tables", []),
    }
    write_text(out / "graph.json", json.dumps(graph, indent=2) + "\n")
    write_text(out / "templates.json", json.dumps({"templates": prediction["templates"]}, indent=2) + "\n")


def with_population(resolved: dict[str, Any], build_dir: Path) -> dict[str, Any]:
    """The resolved pack with the population ids the build found, so pack.json readers always see `ids`."""
    graph = build_dir / "prediction" / "graph.json"
    if "prediction" in resolved and graph.is_file():
        population = json.loads(graph.read_text(encoding="utf-8"))["population"]
        resolved["prediction"] = resolved["prediction"] | {"population": population}
    return resolved


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
