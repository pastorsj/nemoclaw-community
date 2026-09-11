#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Activate one bundled or built dataset for an isolated Query Claw run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any


MANIFEST_NAME = "active-dataset.json"
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ActivationError(ValueError):
    """A dataset cannot be activated safely."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ActivationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load(path: Path, label: str = "dataset manifest") -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ActivationError(f"invalid JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"could not read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must contain a JSON object")
    return value


def _validate_yaml(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ActivationError(f"could not read ontology model: {path}") from exc
    if not content.strip() or "\0" in content:
        raise ActivationError("ontology model must be non-empty UTF-8 YAML")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ActivationError(f"{label} must be a non-empty trimmed string")
    return value


def _relative(value: Any, label: str) -> str:
    raw = _text(value, label)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in raw
    ):
        raise ActivationError(f"{label} must be a contained relative POSIX path")
    return raw


def _source(root: Path, relative: str, label: str, *, directory: bool) -> Path:
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ActivationError(f"{label} must not traverse a symlink")
    try:
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ActivationError(f"{label} must exist inside the repository") from exc
    if candidate.is_dir() != directory or (not directory and not candidate.is_file()):
        kind = "directory" if directory else "file"
        raise ActivationError(f"{label} must be a regular {kind}")
    return candidate


def _copy_file(source: Path, target: Path) -> None:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ActivationError(f"source is not a regular file: {source}")
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with (
            os.fdopen(descriptor, "rb", closefd=False) as incoming,
            target.open("xb") as outgoing,
        ):
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        target.chmod(0o640)
    finally:
        os.close(descriptor)


def _copy_tree(source: Path, target: Path, label: str) -> None:
    # Preserve links rather than following them into staging, then reject them.
    shutil.copytree(source, target, symlinks=True)
    files = 0
    for directory, names, filenames in os.walk(target, followlinks=False):
        current = Path(directory)
        current.chmod(0o750)
        children = [current / name for name in (*names, *filenames)]
        if any(path.is_symlink() for path in children):
            raise ActivationError(f"{label} must not contain symlinks")
        if any(not path.is_dir() for path in (current / name for name in names)):
            raise ActivationError(f"{label} contains an invalid directory")
        for name in filenames:
            path = current / name
            if not path.is_file():
                raise ActivationError(f"{label} contains a non-regular file")
            path.chmod(0o640)
            files += 1
    if not files:
        raise ActivationError(f"{label} must contain at least one file")


def _fingerprint(metadata: dict[str, Any], staging: Path) -> str:
    digest = hashlib.sha256(b"query-claw-active-dataset-v1\0")
    digest.update(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    for path in sorted(
        item
        for item in staging.rglob("*")
        if item.is_file() and item != staging / MANIFEST_NAME
    ):
        relative = path.relative_to(staging).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big") + relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _activation_matches(destination: Path, expected: dict[str, Any]) -> bool:
    """Keep a live bind mount intact when the activation is already exact."""

    manifest = destination / MANIFEST_NAME
    if not destination.is_dir() or manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        if any(path.is_symlink() for path in destination.rglob("*")):
            return False
        if _load(manifest, "active dataset manifest") != expected:
            return False
        fingerprint_metadata = dict(expected)
        expected_fingerprint = fingerprint_metadata.pop("fingerprint")
        return _fingerprint(fingerprint_metadata, destination) == expected_fingerprint
    except (ActivationError, OSError):
        return False


def _manifest(dataset_json: Path, root: Path) -> Path:
    candidate = dataset_json if dataset_json.is_absolute() else root / dataset_json
    if candidate.is_symlink():
        raise ActivationError("dataset manifest must not be a symlink")
    try:
        relative = candidate.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise ActivationError("dataset manifest must be inside the repository") from exc
    return _source(root, relative, "dataset manifest", directory=False)


def _publish(staging: Path, destination: Path) -> None:
    backup: Path | None = None
    if destination.exists():
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.previous-", dir=destination.parent
            )
        )
        backup.rmdir()
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None:
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def _materialize(
    metadata: dict[str, Any],
    sources: tuple[tuple[Path, str, bool], ...],
    destination: Path,
) -> Path:
    """Copy normalized artifacts and atomically publish one active dataset."""

    destination = destination.absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ActivationError("activation destination must be a regular directory")
    for source, _, _ in sources:
        if (
            source == destination
            or source in destination.parents
            or destination in source.parents
        ):
            raise ActivationError(
                "activation destination must not overlap selected outputs"
            )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        staging.chmod(0o750)
        for source, relative, directory in sources:
            target = staging.joinpath(*PurePosixPath(relative).parts)
            if directory:
                _copy_tree(source, target, relative)
            else:
                _copy_file(source, target)
        metadata["fingerprint"] = _fingerprint(metadata, staging)
        if _activation_matches(destination, metadata):
            shutil.rmtree(staging)
            return destination / MANIFEST_NAME
        manifest = staging / MANIFEST_NAME
        manifest.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest.chmod(0o640)
        _publish(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination / MANIFEST_NAME


def activate_external(
    dataset_json: Path, repository_root: Path, destination: Path
) -> Path:
    """Validate and activate one built AIQ3 dataset."""

    if repository_root.is_symlink() or not repository_root.is_dir():
        raise ActivationError("repository root must be a regular directory")
    root = repository_root.resolve(strict=True)
    document = _load(_manifest(dataset_json, root))
    if document.get("schema_version") != "1.0":
        raise ActivationError("dataset schema_version must be '1.0'")

    dataset_id = _text(document.get("id"), "id")
    industry = _object(document.get("industry"), "industry")
    industry_id = _text(industry.get("id"), "industry.id")
    if not _ID.fullmatch(dataset_id) or not _ID.fullmatch(industry_id):
        raise ActivationError("dataset and industry ids must be safe kebab-case")

    build = _object(document.get("build"), "build")
    raw_outputs = build.get("outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ActivationError("build.outputs must be a non-empty list")
    outputs: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_outputs):
        output_item = _object(raw, f"build.outputs[{index}]")
        output_id = _text(output_item.get("id"), f"build.outputs[{index}].id")
        if output_id in outputs:
            raise ActivationError(f"duplicate build output id: {output_id}")
        _text(output_item.get("kind"), f"build.outputs[{index}].kind")
        _text(output_item.get("format"), f"build.outputs[{index}].format")
        _relative(output_item.get("path"), f"build.outputs[{index}].path")
        outputs[output_id] = output_item

    def output(
        reference: Any, label: str, kind: str, format_name: str, *, directory=False
    ) -> Path:
        output_id = _text(reference, label)
        if output_id not in outputs:
            raise ActivationError(f"{label} must reference a declared build output")
        selected = outputs[output_id]
        if selected["kind"] != kind or selected["format"] != format_name:
            raise ActivationError(f"{label} references an incompatible build output")
        relative = _relative(selected["path"], f"build output {output_id}.path")
        return _source(root, relative, f"build output {output_id}", directory=directory)

    database = database_source = ontology = ontology_source = None
    prediction = graph_source = pql_source = None
    prediction_exists = False
    structured = document.get("structured")
    if structured is not None:
        structured = _object(structured, "structured")
        database_source = output(
            structured.get("database_output"),
            "structured.database_output",
            "database",
            "duckdb",
        )
        descriptor = os.open(
            database_source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            valid_duckdb = os.read(descriptor, 12)[8:12] == b"DUCK"
        finally:
            os.close(descriptor)
        if database_source.suffix.casefold() != ".duckdb" or not valid_duckdb:
            raise ActivationError("structured database output must be a DuckDB file")
        database_name = _text(
            structured.get("database_name"), "structured.database_name"
        )
        if not _NAME.fullmatch(database_name):
            raise ActivationError("structured.database_name is invalid")
        database = {
            "engine": "duckdb",
            "name": database_name,
            "path": f"database/{database_name}.duckdb",
        }

        ontology_source = output(
            structured.get("ontology_output"),
            "structured.ontology_output",
            "ontology-model",
            "yaml",
        )
        _validate_yaml(ontology_source)
        ontology = {"path": "ontology/model.gsf.yaml"}

        prediction_contract = structured.get("prediction")
        if prediction_contract is not None:
            prediction_contract = _object(prediction_contract, "structured.prediction")
            graph_source = output(
                prediction_contract.get("graph_output"),
                "structured.prediction.graph_output",
                "prediction-graph",
                "json",
            )
            pql_source = output(
                prediction_contract.get("pql_examples_output"),
                "structured.prediction.pql_examples_output",
                "pql-examples",
                "json",
            )
            _load(graph_source, "prediction graph")
            _load(pql_source, "PQL examples")
            prediction = {
                "mode": "reviewed",
                "graph_path": "prediction/graph.json",
                "pql_examples_path": "prediction/pql-examples.json",
            }
            prediction_exists = True

    documents = documents_source = None
    documents_contract = document.get("documents")
    if documents_contract is not None:
        documents_contract = _object(documents_contract, "documents")
        output_id = _text(
            documents_contract.get("corpus_output"), "documents.corpus_output"
        )
        if output_id not in outputs:
            raise ActivationError(
                "documents.corpus_output must reference a declared build output"
            )
        selected = outputs[output_id]
        if selected["kind"] != "documents":
            raise ActivationError(
                "documents.corpus_output references an incompatible build output"
            )
        documents_source = _source(
            root,
            _relative(selected["path"], f"build output {output_id}.path"),
            f"build output {output_id}",
            directory=True,
        )
        collection = _text(documents_contract.get("collection"), "documents.collection")
        if not _NAME.fullmatch(collection):
            raise ActivationError("documents.collection is invalid")
        documents = {"collection": collection, "path": "documents"}
    if database is None and documents is None:
        raise ActivationError(
            "dataset must declare a DuckDB database or document corpus"
        )

    questions: dict[str, str] = {}
    if document.get("evaluation") is not None:
        evaluation = _object(document["evaluation"], "evaluation")
        for field in ("question_matrix", "stateful_cases"):
            if field in evaluation:
                relative = _relative(evaluation[field], f"evaluation.{field}")
                _source(root, relative, f"evaluation.{field}", directory=False)
                questions[field] = relative

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "id": dataset_id,
        "title": _text(document.get("name"), "name"),
        "industry": {
            "id": industry_id,
            "title": _text(industry.get("name"), "industry.name"),
        },
        "database": database,
        "ontology": ontology,
        "documents": documents,
        "prediction": prediction,
        "prediction_contract_exists": prediction_exists,
        "evaluation_question_paths": questions,
    }

    sources = tuple(
        item
        for item in (
            (database_source, database["path"], False) if database_source else None,
            (ontology_source, "ontology/model.gsf.yaml", False)
            if ontology_source
            else None,
            (graph_source, "prediction/graph.json", False) if graph_source else None,
            (pql_source, "prediction/pql-examples.json", False) if pql_source else None,
            (documents_source, "documents", True) if documents_source else None,
        )
        if item is not None
    )
    return _materialize(metadata, sources, destination)


def activate_bundled(generated: Path, destination: Path) -> Path:
    """Activate the generated supply-chain sample through the same contract."""

    if generated.is_symlink() or not generated.is_dir():
        raise ActivationError("generated data root must be a regular directory")
    root = generated.resolve(strict=True)
    structured = _source(
        root, "service/structured", "generated structured data", directory=True
    )
    documents = _source(
        root, "service/documents", "generated documents", directory=True
    )
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "id": "supply-chain",
        "title": "Synthetic Supply Chain",
        "industry": {"id": "manufacturing", "title": "Manufacturing"},
        "database": {
            "engine": "postgres-csv",
            "name": "query_claw",
            "path": "structured",
        },
        "ontology": None,
        "documents": {
            "collection": "query-claw-supply-chain",
            "path": "documents",
        },
        "prediction": {"mode": "native"},
        "prediction_contract_exists": True,
        "evaluation_question_paths": {},
    }
    return _materialize(
        metadata,
        ((structured, "structured", True), (documents, "documents", True)),
        destination,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--generated", type=Path)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.dataset is not None:
            if args.repository_root is None:
                parser.error("--repository-root is required with --dataset")
            manifest = activate_external(
                args.dataset, args.repository_root, args.output
            )
        else:
            if args.repository_root is not None:
                parser.error("--repository-root may be used only with --dataset")
            manifest = activate_bundled(args.generated, args.output)
    except ActivationError as exc:
        parser.error(str(exc))
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
