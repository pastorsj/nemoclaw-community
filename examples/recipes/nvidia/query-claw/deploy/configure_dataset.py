#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inspect and provision one normalized Query Claw dataset activation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DatasetConfigurationError(ValueError):
    """The normalized activation cannot be consumed safely."""


@dataclass(frozen=True)
class ActiveDataset:
    dataset_id: str
    database_engine: str | None
    database_name: str | None
    database_path: Path | None
    ontology_path: Path | None
    documents_path: Path | None
    collection: str | None
    graph_path: Path | None
    pql_path: Path | None
    prediction_mode: str | None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DatasetConfigurationError(f"invalid JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetConfigurationError(f"cannot read {label}: {path}") from exc
    if not isinstance(result, dict):
        raise DatasetConfigurationError(f"{label} must be a JSON object")
    return result


def _text(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DatasetConfigurationError(f"{label} is invalid")
    return value


def _artifact(root: Path, value: Any, expected: str, *, directory=False) -> Path:
    if not isinstance(value, str) or value != expected:
        raise DatasetConfigurationError(
            f"activation must use normalized path {expected}"
        )
    relative = PurePosixPath(value)
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise DatasetConfigurationError(
                f"activation artifact is a symlink: {value}"
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise DatasetConfigurationError(
            f"activation artifact is unavailable: {value}"
        ) from exc
    if resolved.is_dir() != directory or (not directory and not resolved.is_file()):
        raise DatasetConfigurationError(
            f"activation artifact has the wrong type: {value}"
        )
    return resolved


def load_active(manifest_path: Path) -> ActiveDataset:
    """Load the strict one-dataset contract and resolve its staged artifacts."""

    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DatasetConfigurationError(
            "active dataset manifest must be a regular file"
        )
    manifest = manifest_path.resolve(strict=True)
    root = manifest.parent
    document = _load_json(manifest, "active dataset manifest")
    if document.get("schema_version") != 1:
        raise DatasetConfigurationError("active dataset schema_version must be 1")
    if not _SHA256.fullmatch(str(document.get("fingerprint", ""))):
        raise DatasetConfigurationError("active dataset fingerprint is invalid")
    dataset_id = _text(document.get("id"), "active dataset id", _ID)

    database = document.get("database")
    ontology = document.get("ontology")
    documents = document.get("documents")
    database_engine = database_name = database_path = ontology_path = None
    if database is not None:
        if not isinstance(database, dict):
            raise DatasetConfigurationError("database contract must be an object")
        database_engine = database.get("engine")
        if database_engine not in {"duckdb", "postgres-csv"}:
            raise DatasetConfigurationError("database engine is invalid")
        database_name = _text(database.get("name"), "database name", _NAME)
        if database_engine == "duckdb":
            database_path = _artifact(
                root,
                database.get("path"),
                f"database/{database_name}.duckdb",
            )
            if not isinstance(ontology, dict):
                raise DatasetConfigurationError(
                    "DuckDB database requires a reviewed ontology"
                )
            ontology_path = _artifact(
                root, ontology.get("path"), "ontology/model.gsf.yaml"
            )
        else:
            database_path = _artifact(
                root, database.get("path"), "structured", directory=True
            )
            if ontology is not None:
                raise DatasetConfigurationError(
                    "postgres-csv database uses GSF-native semantic compilation"
                )
    elif ontology is not None:
        raise DatasetConfigurationError("ontology requires a database contract")

    documents_path = collection = None
    if documents is not None:
        if not isinstance(documents, dict):
            raise DatasetConfigurationError("documents contract must be an object")
        documents_path = _artifact(
            root, documents.get("path"), "documents", directory=True
        )
        collection = _text(
            documents.get("collection"), "Retriever collection", _NAME
        )
    if database is None and documents is None:
        raise DatasetConfigurationError(
            "active dataset requires structured data or documents"
        )

    prediction = document.get("prediction")
    exists = document.get("prediction_contract_exists")
    if (prediction is not None) != (exists is True):
        raise DatasetConfigurationError("prediction contract state is inconsistent")
    graph_path = pql_path = prediction_mode = None
    if prediction is not None:
        if database is None:
            raise DatasetConfigurationError(
                "prediction contract requires a structured database"
            )
        if not isinstance(prediction, dict):
            raise DatasetConfigurationError("prediction contract must be an object")
        prediction_mode = prediction.get("mode")
        if prediction_mode == "reviewed":
            if database_engine != "duckdb":
                raise DatasetConfigurationError(
                    "reviewed prediction requires a DuckDB database"
                )
            graph_path = _artifact(
                root, prediction.get("graph_path"), "prediction/graph.json"
            )
            pql_path = _artifact(
                root,
                prediction.get("pql_examples_path"),
                "prediction/pql-examples.json",
            )
        elif prediction_mode == "native":
            if database_engine != "postgres-csv":
                raise DatasetConfigurationError(
                    "native prediction requires a postgres-csv database"
                )
        else:
            raise DatasetConfigurationError("prediction mode is invalid")
    elif exists is not False:
        raise DatasetConfigurationError("prediction_contract_exists must be boolean")

    return ActiveDataset(
        dataset_id=dataset_id,
        database_engine=database_engine,
        database_name=database_name,
        database_path=database_path,
        ontology_path=ontology_path,
        documents_path=documents_path,
        collection=collection,
        graph_path=graph_path,
        pql_path=pql_path,
        prediction_mode=prediction_mode,
    )


def _base_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DatasetConfigurationError("GSF base URL must be a loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DatasetConfigurationError("GSF base URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise DatasetConfigurationError("GSF base URL must include a valid port")
    return value.rstrip("/")


def _request(url: str, body: bytes, content_type: str) -> dict[str, Any]:
    request = Request(
        url,
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=900) as response:  # noqa: S310 - loopback only
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DatasetConfigurationError(
            f"GSF request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetConfigurationError(f"GSF request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise DatasetConfigurationError("GSF returned a non-object response")
    return result


def import_ontology(active: ActiveDataset, base_url: str) -> None:
    if active.ontology_path is None or active.database_name is None:
        raise DatasetConfigurationError("active dataset has no structured ontology")
    result = _request(
        f"{_base_url(base_url)}/api/model/import?replace=true&embed=true",
        active.ontology_path.read_bytes(),
        "application/x-yaml",
    )
    if result.get("success") is not True:
        raise DatasetConfigurationError("GSF did not confirm ontology import")


def _pql_examples(active: ActiveDataset) -> list[dict[str, Any]]:
    document = _load_json(active.pql_path, "PQL examples")
    examples = document.get("examples")
    if (
        document.get("schema_version") != 1
        or document.get("database_name") != active.database_name
        or not isinstance(examples, list)
        or not examples
    ):
        raise DatasetConfigurationError("PQL examples do not match the active database")
    return examples


def seed_pql(active: ActiveDataset, base_url: str) -> int:
    if active.pql_path is None:
        return 0
    examples = _pql_examples(active)
    names: set[str] = set()
    pql_values: set[str] = set()
    endpoint = f"{_base_url(base_url)}/api/pql-analyses"
    for index, item in enumerate(examples):
        if not isinstance(item, dict):
            raise DatasetConfigurationError(f"PQL example {index} must be an object")
        payload: dict[str, str] = {}
        for field in ("name", "description", "pql"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise DatasetConfigurationError(
                    f"PQL example {index}.{field} is invalid"
                )
            payload[field] = value.strip()
        if payload["name"] in names or payload["pql"] in pql_values:
            raise DatasetConfigurationError(
                "PQL examples contain duplicate names or queries"
            )
        names.add(payload["name"])
        pql_values.add(payload["pql"])
        result = _request(
            endpoint,
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json",
        )
        if not isinstance(result.get("data"), dict) or not result["data"].get("id"):
            raise DatasetConfigurationError("GSF did not confirm PQL example creation")
    return len(examples)


def prediction_context(active: ActiveDataset) -> dict[str, Any]:
    """Select the reviewed prediction facts useful for routing."""

    if active.prediction_mode != "reviewed":
        return {}
    graph = _load_json(active.graph_path, "prediction graph")
    examples = _pql_examples(active)
    try:
        scope = graph.get("prediction_scope")
        targets = [item["name"].strip() for item in examples]
        if (
            graph["schema_version"] != 1
            or graph["database_name"] != active.database_name
            or not 1 <= len(targets) <= 8
            or any(not target or len(target) > 160 for target in targets)
        ):
            raise KeyError
        selected_scope = (
            None
            if scope is None
            else {
                "anchor_time": scope["anchor_time"],
                "entity": ".".join(
                    _text(scope[key], f"prediction scope {key}", _NAME)
                    for key in ("entity_table", "entity_column")
                ),
                "population": ".".join(
                    _text(scope[key], f"prediction scope {key}", _NAME)
                    for key in ("population_view", "population_column")
                ),
                "population_count": scope["population_rows"],
            }
        )
        if selected_scope is not None and (
            not isinstance(selected_scope["anchor_time"], str)
            or len(selected_scope["anchor_time"]) > 40
            or isinstance(selected_scope["population_count"], bool)
            or not 1 <= selected_scope["population_count"] <= 10_000_000
        ):
            raise KeyError
    except (AttributeError, KeyError, TypeError) as exc:
        raise DatasetConfigurationError(
            "reviewed prediction context is invalid"
        ) from exc
    return {"scope": selected_scope, "targets": targets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    field = subparsers.add_parser("field")
    field.add_argument(
        "name",
        choices=(
            "has-structured",
            "has-prediction",
            "has-documents",
            "database-name",
            "database-engine",
            "database-path",
            "retriever-map",
            "prediction-context",
        ),
    )
    for action in ("import-ontology", "seed-pql"):
        command = subparsers.add_parser(action)
        command.add_argument("--base-url", default="http://127.0.0.1:3001")
    args = parser.parse_args()
    try:
        active = load_active(args.manifest)
        if args.action == "field":
            if args.name == "has-structured":
                print("1" if active.database_path is not None else "0")
            elif args.name == "has-prediction":
                print("1" if active.prediction_mode is not None else "0")
            elif args.name == "has-documents":
                print("1" if active.documents_path is not None else "0")
            elif args.name == "database-name":
                if active.database_name is None:
                    raise DatasetConfigurationError(
                        "active dataset has no structured database"
                    )
                print(active.database_name)
            elif args.name == "database-engine":
                if active.database_engine is None:
                    raise DatasetConfigurationError(
                        "active dataset has no structured database"
                    )
                print(active.database_engine)
            elif args.name == "database-path":
                if active.database_path is None:
                    raise DatasetConfigurationError(
                        "active dataset has no structured database"
                    )
                print(active.database_path.relative_to(args.manifest.resolve().parent))
            elif args.name == "retriever-map":
                mapping = (
                    {active.dataset_id: active.collection}
                    if active.collection is not None
                    else {}
                )
                print(
                    json.dumps(
                        mapping,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            else:
                print(
                    json.dumps(
                        prediction_context(active),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        elif args.action == "import-ontology":
            import_ontology(active, args.base_url)
            print(f"imported: {active.database_name}")
        else:
            print(f"seeded: {seed_pql(active, args.base_url)}")
    except DatasetConfigurationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
