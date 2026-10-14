# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Import the active data pack's ontology into Auto Ontology, and seed it again whenever the model changes.

The model is derived by `demo-data prepare` (ontology/model.yaml), so Auto Ontology's own LLM compiler never runs.
Run it after the backend is healthy and before ingestion first starts, so ingestion converges onto the imported IDs
instead of cataloging the database a second time.

Auto Ontology's import (`POST /api/model/import?replace=true`) creates what the catalog lacks and drops the
semantics the model no longer names, but it never updates an entity it already has: a new description of a table,
column, term or attribute would never reach the catalog. So the seed keeps the SHA-256 of the model it imported, per
database, in Auto Ontology's own Postgres (the table `market_demo.ontology_seed`, which lives and goes with the
pack's ontology volume). When the model's hash differs from the recorded one, the seed:

1. clears that database with Auto Ontology's own per-database reset (`auto_ontology.dal.reset.delete_all_data`:
   its catalog rows, its semantic rows and their embeddings);
2. imports the model;
3. records the hash;
4. asks the ingestion service, if it is already running, to catalog the database again, as it does when it starts.

Only the databases the model names are reset; nothing else in Auto Ontology is touched. When the hash is unchanged,
the seed imports the model again as before, which creates nothing.

It runs in the backend image (`python /seed.py` with PYTHONPATH=/app), which has PyYAML, SQLAlchemy and the
`auto_ontology` package.

Env: AUTO_ONTOLOGY_API_URL (default http://auto-ontology:3001), AUTO_ONTOLOGY_MODEL
(default /data/active/ontology/model.yaml), AUTO_ONTOLOGY_SEED_TIMEOUT_S (default 600; embedding every term is slow),
INGESTION_SERVICE_URL (optional) and the POSTGRES_* settings the backend reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Protocol

# Ours, beside Auto Ontology's `public` (Alembic), `frontend` (Prisma) and `vdb` (embeddings) schemas.
RECORD = "market_demo.ontology_seed"
RECORD_DDL = (
    "CREATE SCHEMA IF NOT EXISTS market_demo",
    f"CREATE TABLE IF NOT EXISTS {RECORD} ("
    " database_name text PRIMARY KEY,"
    " model_sha256 text NOT NULL,"
    " seeded_at timestamptz NOT NULL DEFAULT now())",
)


class Catalog(Protocol):
    """What the seed needs from Auto Ontology's store, besides the import route."""

    def recorded(self, database: str) -> str | None:
        """The hash of the model last seeded into `database`, if any."""

    def has(self, database: str) -> bool:
        """Whether the catalog holds `database`."""

    def reset(self, database: str) -> None:
        """Delete `database`'s catalog and semantic rows and their embeddings, and nothing else."""

    def record(self, database: str, digest: str) -> None:
        """Remember that the model with this hash is seeded into `database`."""


class AutoOntologyCatalog:
    """Auto Ontology's Postgres, through the backend's own data access layer."""

    def __init__(self) -> None:
        from auto_ontology.dal import schema
        from auto_ontology.dal.reset import delete_all_data
        from auto_ontology.dal.session import store
        from sqlalchemy import select

        self._databases = schema.catalog_database
        self._delete_all_data = delete_all_data
        self._store = store
        self._select = select
        for statement in RECORD_DDL:
            store().query_write(statement)

    def recorded(self, database: str) -> str | None:
        rows = self._store().query_read(
            f"SELECT model_sha256 FROM {RECORD} WHERE database_name = :database", {"database": database}
        )
        return rows[0]["model_sha256"] if rows else None

    def has(self, database: str) -> bool:
        statement = self._select(self._databases.c.id).where(self._databases.c.name == database)
        return bool(self._store().query_read(statement))

    def reset(self, database: str) -> None:
        self._delete_all_data(database)

    def record(self, database: str, digest: str) -> None:
        self._store().query_write(
            f"INSERT INTO {RECORD} (database_name, model_sha256) VALUES (:database, :digest)"
            " ON CONFLICT (database_name) DO UPDATE SET model_sha256 = excluded.model_sha256, seeded_at = now()",
            {"database": database, "digest": digest},
        )


@dataclass
class Seeded:
    digest: str
    databases: list[str]
    reset: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    ingestion: str = ""


def model_digest(model: bytes) -> str:
    return hashlib.sha256(model).hexdigest()


def database_names(model: bytes) -> list[str]:
    """The catalog databases the model imports into, named as the import names them (its first schema's database)."""
    import yaml

    try:
        document = yaml.safe_load(model)
    except yaml.YAMLError as error:
        raise ValueError(f"the model is not YAML: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("the model is not a YAML mapping")
    names: list[str] = []
    for database in (document.get("data_layer") or {}).get("databases") or []:
        schemas = database.get("schemas") or []
        name = (schemas[0].get("database_name") or schemas[0].get("name") or "") if schemas else ""
        name = name or database.get("id") or ""
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError("the model names no database")
    return names


def import_model(api_url: str, model: bytes, timeout: float) -> dict[str, Any]:
    """POST the YAML model with replace=true and embed=true; return the import summary."""
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/model/import?replace=true&embed=true",
        data=model,
        method="POST",
        headers={"Content-Type": "application/x-yaml", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("success") is not True:
        raise RuntimeError(f"the import did not report success: {payload}")
    return payload.get("summary") or {}


def request_ingest(ingestion_url: str, database: str, timeout: float = 10) -> str:
    """Ask a running ingestion service to catalog `database` again, and say what happened. Never raises."""
    request = urllib.request.Request(
        f"{ingestion_url.rstrip('/')}/ingest",
        data=json.dumps({"database": database}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return "requested"
    except urllib.error.HTTPError as error:
        return f"not requested (HTTP {error.code})"
    except OSError:
        return "not running; it catalogs the database when it starts"


def seed(api_url: str, model: bytes, timeout: float, catalog: Catalog, ingestion_url: str = "") -> Seeded:
    """Reset each database whose recorded model hash differs, import the model, then record its hash."""
    seeded = Seeded(model_digest(model), database_names(model))
    seeded.reset = [name for name in seeded.databases if catalog.recorded(name) != seeded.digest and catalog.has(name)]
    for name in seeded.reset:
        catalog.reset(name)
    seeded.summary = import_model(api_url, model, timeout)
    for name in seeded.databases:
        catalog.record(name, seeded.digest)
    if seeded.reset and ingestion_url:
        seeded.ingestion = "; ".join(f"{name}: {request_ingest(ingestion_url, name)}" for name in seeded.reset)
    return seeded


def main(catalog: Catalog | None = None) -> int:
    api_url = os.environ.get("AUTO_ONTOLOGY_API_URL", "http://auto-ontology:3001")
    model = Path(os.environ.get("AUTO_ONTOLOGY_MODEL", "/data/active/ontology/model.yaml"))
    timeout = float(os.environ.get("AUTO_ONTOLOGY_SEED_TIMEOUT_S", "600"))
    ingestion_url = os.environ.get("INGESTION_SERVICE_URL", "")
    try:
        seeded = seed(api_url, model.read_bytes(), timeout, catalog or AutoOntologyCatalog(), ingestion_url)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        print(f"seed: {api_url} rejected {model}: HTTP {error.code} {detail}", file=sys.stderr)
        return 1
    except Exception as error:  # the store's errors too (SQLAlchemy, psycopg): one line, not a traceback
        print(f"seed: could not seed {model} into {api_url}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if seeded.reset:
        print(f"seed: the model changed; reset the catalog of {', '.join(seeded.reset)} before importing it")
    print(
        f"seed: imported {model} (sha256 {seeded.digest[:12]}) into {', '.join(seeded.databases)} at {api_url}: "
        f"{json.dumps(seeded.summary, sort_keys=True)}"
    )
    if seeded.ingestion:
        print(f"seed: ingestion {seeded.ingestion}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
