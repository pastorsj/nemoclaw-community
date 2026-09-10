# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discover and activate trusted Query Claw data packs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


DATASETS_ENV = "QUERY_CLAW_DATASETS"
MANIFEST_NAME = "active-data-packs.json"
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VIEW_BINDING = {
    "structured": "ontology",
    "documents": "retriever",
    "predictions": "ontology",
}


class DataPackError(ValueError):
    """A data-pack definition or selection is invalid."""


@dataclass(frozen=True)
class DataPack:
    """A validated pack rooted at ``data/packs/<id>``."""

    id: str
    root: Path
    definition: Mapping[str, Any]
    files: tuple[str, ...]


@dataclass(frozen=True)
class ActivePacks:
    """The deterministic dataset allowlist for one Query Claw deployment."""

    packs: tuple[DataPack, ...]
    fingerprint: str

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(pack.id for pack in self.packs)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DataPackError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_definition(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DataPackError(f"invalid JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DataPackError(f"could not read {path}: {error}") from error
    if not isinstance(value, dict):
        raise DataPackError(f"{path} must contain a JSON object")
    return value


def _object(
    value: Any,
    label: str,
    fields: set[str],
    required: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DataPackError(f"{label} must be an object")
    unknown = set(value) - fields
    missing = (fields if required is None else required) - set(value)
    if unknown:
        raise DataPackError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise DataPackError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DataPackError(f"{label} must be a non-empty trimmed string")
    if any(ord(character) < 32 for character in value):
        raise DataPackError(f"{label} must not contain control characters")
    return value


def _safe_path(
    root: Path, value: Any, label: str, *, require_file: bool = False
) -> Path:
    raw = _text(value, label)
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in raw
    ):
        raise DataPackError(f"{label} must be a contained relative POSIX path")

    root_real = root.resolve(strict=True)
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise DataPackError(f"{label} must not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_real)
    except (FileNotFoundError, ValueError) as error:
        raise DataPackError(f"{label} must resolve inside its data pack") from error
    if require_file and not resolved.is_file():
        raise DataPackError(f"{label} must name a regular file")
    if not require_file and not (resolved.is_file() or resolved.is_dir()):
        raise DataPackError(f"{label} must name a regular file or directory")
    return resolved


def _validate_definition(pack_dir: Path, value: dict[str, Any]) -> None:
    root_fields = {
        "schema_version",
        "id",
        "title",
        "description",
        "industry",
        "views",
        "bindings",
    }
    root = _object(
        value,
        "pack",
        root_fields,
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise DataPackError("schema_version must be 1")
    pack_id = _text(root["id"], "id")
    if not _ID.fullmatch(pack_id) or pack_id != pack_dir.name:
        raise DataPackError("id must be safe kebab-case and match its directory")
    for key in ("title", "description", "industry"):
        _text(root[key], key)

    views = _object(root["views"], "views", set(_VIEW_BINDING), set())
    if not views:
        raise DataPackError("views must contain at least one dataset view")
    bindings = _object(root["bindings"], "bindings", {"ontology", "retriever"}, set())
    required_bindings = {_VIEW_BINDING[view] for view in views}
    if set(bindings) != required_bindings:
        raise DataPackError("bindings must exactly match the configured views")
    for view, path in views.items():
        _safe_path(pack_dir, path, f"views.{view}")

    if "ontology" in bindings:
        ontology_fields = {"database", "prediction_database", "prediction_probe"}
        ontology_required = {"database"}
        if "predictions" in views:
            ontology_required.add("prediction_probe")
        ontology = _object(
            bindings["ontology"],
            "bindings.ontology",
            ontology_fields,
            ontology_required,
        )
        for field in ontology:
            text = _text(ontology[field], f"bindings.ontology.{field}")
            if field != "prediction_probe" and not _NAME.fullmatch(text):
                raise DataPackError(
                    f"bindings.ontology.{field} must be a safe service name"
                )
        if len(ontology.get("prediction_probe", "")) > 2_000:
            raise DataPackError(
                "bindings.ontology.prediction_probe must not exceed 2000 characters"
            )
    if "retriever" in bindings:
        retriever = _object(bindings["retriever"], "bindings.retriever", {"collection"})
        collection = _text(retriever["collection"], "bindings.retriever.collection")
        if not _NAME.fullmatch(collection):
            raise DataPackError(
                "bindings.retriever.collection must be a safe service name"
            )


def _view_files(pack_dir: Path, definition: Mapping[str, Any]) -> tuple[str, ...]:
    files = {"pack.json"}
    pack_root = pack_dir.resolve(strict=True)
    roots = {
        _safe_path(pack_dir, relative, f"views.{view}")
        for view, relative in definition["views"].items()
    }
    for root in roots:
        if root.is_file():
            files.add(root.relative_to(pack_root).as_posix())
            continue
        for directory, names, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            for name in names:
                if (current / name).is_symlink():
                    raise DataPackError("data-pack views must not contain symlinks")
            for name in filenames:
                path = current / name
                if path.is_symlink():
                    raise DataPackError("data-pack views must not contain symlinks")
                if not path.is_file():
                    raise DataPackError(
                        "data-pack views may contain only regular files"
                    )
                files.add(path.relative_to(pack_root).as_posix())
    return tuple(sorted(files))


def discover_packs(packs_root: Path | str) -> dict[str, DataPack]:
    """Discover and validate direct ``<id>/pack.json`` children."""

    root = Path(packs_root)
    if root.is_symlink() or not root.is_dir():
        raise DataPackError("data-pack root must be a non-symlink directory")
    packs: dict[str, DataPack] = {}
    for pack_dir in sorted(root.iterdir(), key=lambda path: path.name):
        if pack_dir.is_symlink():
            raise DataPackError("data-pack root must not contain symlinks")
        manifest = pack_dir / "pack.json"
        if not pack_dir.is_dir() or not manifest.exists():
            continue
        if manifest.is_symlink() or not manifest.is_file():
            raise DataPackError("pack.json must be a regular non-symlink file")
        definition = _read_definition(manifest)
        _validate_definition(pack_dir, definition)
        pack_id = definition["id"]
        if pack_id in packs:
            raise DataPackError("duplicate data-pack id")
        packs[pack_id] = DataPack(
            id=pack_id,
            root=pack_dir.resolve(strict=True),
            definition=definition,
            files=_view_files(pack_dir, definition),
        )
    return packs


def parse_dataset_allowlist(
    value: str | None = None, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Parse ``QUERY_CLAW_DATASETS`` as a non-empty, unique ID set."""

    if value is None:
        value = (os.environ if environ is None else environ).get(DATASETS_ENV)
    if value is None or not value.strip():
        raise DataPackError(f"{DATASETS_ENV} must select at least one dataset")
    pieces = [piece.strip() for piece in value.split(",")]
    if any(not piece for piece in pieces):
        raise DataPackError(f"{DATASETS_ENV} contains an empty dataset id")
    if any(not _ID.fullmatch(piece) for piece in pieces):
        raise DataPackError(f"{DATASETS_ENV} contains an invalid dataset id")
    if len(pieces) != len(set(pieces)):
        raise DataPackError(f"{DATASETS_ENV} contains a duplicate dataset id")
    return tuple(sorted(pieces))


def _fingerprint(packs: tuple[DataPack, ...]) -> str:
    digest = hashlib.sha256(b"query-claw-data-packs-v1\0")
    for pack in packs:
        digest.update(pack.id.encode("utf-8") + b"\0")
        for relative in pack.files:
            path = _safe_path(
                pack.root, relative, "fingerprint file", require_file=True
            )
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def select_packs(
    registry: Mapping[str, DataPack],
    value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ActivePacks:
    """Select only explicitly allowed packs and fingerprint their content."""

    ids = parse_dataset_allowlist(value, environ)
    try:
        packs = tuple(registry[pack_id] for pack_id in ids)
    except KeyError as error:
        raise DataPackError("requested dataset is unavailable") from error
    bindings: dict[tuple[str, str], str] = {}
    for pack in packs:
        definition = pack.definition["bindings"]
        resources: set[tuple[str, str]] = set()
        if ontology := definition.get("ontology"):
            resources.add(("ontology", ontology["database"]))
            if "predictions" in pack.definition["views"]:
                resources.add(
                    (
                        "ontology",
                        ontology.get("prediction_database", ontology["database"]),
                    )
                )
        if retriever := definition.get("retriever"):
            resources.add(("retriever", retriever["collection"]))
        for resource in resources:
            if resource in bindings and bindings[resource] != pack.id:
                raise DataPackError("selected data packs share a service binding")
            bindings[resource] = pack.id
    return ActivePacks(packs=packs, fingerprint=_fingerprint(packs))


def _assert_plain_tree(root: Path) -> None:
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        directories = [current / name for name in names]
        files = [current / name for name in filenames]
        if any(path.is_symlink() for path in (*directories, *files)):
            raise DataPackError("active-pack output must not contain symlinks")
        if any(not path.is_dir() for path in directories):
            raise DataPackError("active-pack output contains an invalid directory")
        if any(not path.is_file() for path in files):
            raise DataPackError("active-pack output contains a non-regular file")


def _active_definition(pack: DataPack) -> dict[str, Any]:
    definition = json.loads(json.dumps(pack.definition))
    definition["views"] = {
        name: f"packs/{pack.id}/{path}" for name, path in definition["views"].items()
    }
    return definition


def materialize_active_packs(selection: ActivePacks, output_dir: Path | str) -> Path:
    """Replace an active tree with selected pack files and a portable manifest."""

    output = Path(output_dir)
    if (
        not output.name
        or output.is_symlink()
        or (output.exists() and not output.is_dir())
    ):
        raise DataPackError("active-pack output must be a non-symlink directory")
    if output.exists():
        _assert_plain_tree(output)
    output_real = output.resolve(strict=False)
    for pack in selection.packs:
        if (
            output_real == pack.root
            or output_real in pack.root.parents
            or pack.root in output_real.parents
        ):
            raise DataPackError("active-pack output must not overlap source packs")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    )
    backup: Path | None = None
    try:
        copied: list[DataPack] = []
        for pack in selection.packs:
            destination = staging / "packs" / pack.id
            for view_path in pack.definition["views"].values():
                source = _safe_path(pack.root, view_path, "selected pack view")
                if source.is_dir():
                    destination.joinpath(*PurePosixPath(view_path).parts).mkdir(
                        parents=True, exist_ok=True
                    )
            for relative in pack.files:
                source = _safe_path(
                    pack.root, relative, "selected pack file", require_file=True
                )
                target = destination.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
            copied.append(DataPack(pack.id, destination, pack.definition, pack.files))
        if _fingerprint(tuple(copied)) != selection.fingerprint:
            raise DataPackError("selected pack changed during materialization")

        payload = {
            "schema_version": 1,
            "fingerprint": selection.fingerprint,
            "datasets": [_active_definition(pack) for pack in selection.packs],
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if output.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{output.name}.previous-", dir=output.parent)
            )
            backup.rmdir()
            output.replace(backup)
        try:
            staging.replace(output)
        except Exception:
            if backup is not None:
                backup.replace(output)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output / MANIFEST_NAME
