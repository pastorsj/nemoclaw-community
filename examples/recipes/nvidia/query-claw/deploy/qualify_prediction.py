#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Qualify every active Kumo prediction view through NVIDIA Ontology."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _prediction_targets(
    manifest_path: Path,
) -> tuple[str, list[tuple[str, str, str]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise RuntimeError("active data-pack fingerprint is invalid")
    targets = []
    for dataset in manifest.get("datasets", []):
        if "predictions" not in dataset.get("views", {}):
            continue
        ontology = dataset.get("bindings", {}).get("ontology", {})
        database = ontology.get("prediction_database") or ontology.get("database")
        values = (dataset.get("id"), database, ontology.get("prediction_probe"))
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise RuntimeError("active predictive data-pack metadata is invalid")
        if len(values[2]) > 2_000:
            raise RuntimeError("active prediction probe is too long")
        targets.append(values)
    if not targets:
        raise RuntimeError("no active predictive data pack can qualify Kumo")
    return fingerprint, targets


def _result(url: str, database: str, question: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/chat/completions",
        data=json.dumps(
            {"question": question, "prediction": True, "target_db": database}
        ).encode(),
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        method="POST",
    )
    answer = None
    with urllib.request.urlopen(request, timeout=900) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if event.get("type") == "error":
                raise RuntimeError("NVIDIA Ontology prediction returned an error")
            if event.get("type") == "result":
                answer = event.get("answer") or event
    if not isinstance(answer, dict):
        raise RuntimeError("NVIDIA Ontology prediction ended without a result")
    return answer


def _validate_prediction(answer: dict[str, Any], database: str) -> None:
    rows = answer.get("rows") or answer.get("sql_response_from_db")
    if isinstance(rows, str) or (
        isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], str)
    ):
        encoded = rows if isinstance(rows, str) else rows[0]
        try:
            rows = json.loads(encoded)
        except json.JSONDecodeError:
            rows = None
    query = answer.get("pql") or answer.get("pql_code") or answer.get("sql_code")
    graph = answer.get("graph_receipt")
    if not isinstance(query, str) or not re.match(r"^\s*PREDICT\b", query, re.I):
        raise RuntimeError("NVIDIA Ontology returned no qualified Kumo prediction")
    if (
        not isinstance(rows, list)
        or not rows
        or any(not isinstance(row, dict) for row in rows)
        or not isinstance(graph, dict)
        or graph.get("database_name") != database
    ):
        raise RuntimeError("NVIDIA Ontology returned no qualified Kumo prediction")


def _write_receipt(path: Path, fingerprint: str, databases: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                {
                    "schema_version": 1,
                    "selection_fingerprint": fingerprint,
                    "databases": sorted(databases),
                },
                stream,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:3001")
    args = parser.parse_args()

    fingerprint, targets = _prediction_targets(args.manifest)
    databases = []
    for dataset_id, database, prediction_probe in targets:
        answer = _result(args.url, database, prediction_probe)
        try:
            _validate_prediction(answer, database)
        except RuntimeError as exc:
            raise RuntimeError(
                f"NVIDIA Ontology returned no qualified Kumo prediction for {dataset_id}"
            ) from exc
        databases.append(database)
        print(f"ready: NVIDIA Ontology prediction for {dataset_id} ({database})")
    _write_receipt(args.receipt, fingerprint, databases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
