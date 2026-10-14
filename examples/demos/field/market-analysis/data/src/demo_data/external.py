# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""External datasets: data that lives outside the repository and is pinned by its manifest's fingerprint.

pack.yaml declares each one under `external.<id>`: the path of its manifest inside the dataset, the pinned
`fingerprint` and its size. The dataset itself lives in $DATA_SOURCE_DIR/<id>/ and is never committed.

The manifest lists every file with its size and hash; keys other than `files` are informational:

    {"files": [{"path": "market/stocks_1min/AAPL_full_1min_adjsplit.parquet", "bytes": 3865509, "sha256": "..."}]}

    fingerprint = sha256(json.dumps(files, sort_keys=True, separators=(",", ":")))

Verification has two levels. `verify` hashes every file whose size or mtime changed since the last run and
records the result in <dataset>/.verified.json, so a rerun is cheap and an interrupted fetch resumes.
`require_verified` only compares sizes and mtimes with that record, for `prepare`, which sees the data read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from demo_data.corpus.common import sha256_file

RECORD = ".verified.json"


class ExternalError(Exception):
    """An external dataset is missing, is not the pinned one, or failed verification."""


@dataclass(frozen=True)
class Dataset:
    id: str
    root: Path  # $DATA_SOURCE_DIR/<id>
    manifest: str  # the manifest's path inside the dataset
    fingerprint: str
    bytes: int

    @property
    def manifest_path(self) -> Path:
        return self.root / self.manifest

    @property
    def source_variable(self) -> str:
        """The .env variable naming where `fetch` gets this dataset: DATA_SOURCE_<ID>."""
        return "DATA_SOURCE_" + self.id.upper().replace("-", "_")

    def files(self) -> list[dict[str, Any]]:
        """The manifest's file list, once its fingerprint matches the pin."""
        if not self.manifest_path.is_file():
            raise ExternalError(
                f"{self.id}: {self.manifest_path} not found; fetch it with `demo.sh data fetch` "
                f"(its source is {self.source_variable})"
            )
        return parse_manifest(self.manifest_path.read_bytes(), self)


@dataclass
class Report:
    """What a verification found."""

    files: int = 0
    bytes: int = 0
    hashed: int = 0
    bad: list[str] = field(default_factory=list)  # missing, or the wrong size or content
    extra: list[str] = field(default_factory=list)  # present but not in the manifest (never deleted)


def datasets(manifest: dict[str, Any], sources_dir: Path) -> dict[str, Dataset]:
    """The pack's external datasets, each rooted in `sources_dir`."""
    return {
        dataset_id: Dataset(dataset_id, sources_dir / dataset_id, spec["manifest"], spec["fingerprint"], spec["bytes"])
        for dataset_id, spec in manifest.get("external", {}).items()
    }


def fingerprint(files: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_manifest(content: bytes, dataset: Dataset) -> list[dict[str, Any]]:
    """The manifest's `files`, checked against the pinned fingerprint and for safe relative paths."""
    try:
        files = json.loads(content)["files"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ExternalError(f"{dataset.id}: {dataset.manifest} is not a dataset manifest ({error})") from error
    actual = fingerprint(files)
    if actual != dataset.fingerprint:
        raise ExternalError(
            f"{dataset.id}: the manifest's fingerprint is {actual}, but pack.yaml pins {dataset.fingerprint}: "
            "this is a different dataset"
        )
    for entry in files:
        path = PurePosixPath(entry["path"])
        if path.is_absolute() or ".." in path.parts or str(path) in ("", ".", dataset.manifest):
            raise ExternalError(f"{dataset.id}: unsafe path in the manifest: {entry['path']!r}")
    return files


def load_record(dataset: Dataset) -> dict[str, list[Any]]:
    """path -> [bytes, mtime_ns, sha256] for every file already verified."""
    path = dataset.root / RECORD
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_record(dataset: Dataset, record: dict[str, list[Any]]) -> None:
    temporary = dataset.root / f".{RECORD}.tmp"
    temporary.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, dataset.root / RECORD)


def recorded(record: dict[str, list[Any]], root: Path, entry: dict[str, Any]) -> bool:
    """The file is still the one recorded with the manifest's hash: same size and mtime, no need to hash it."""
    seen = record.get(entry["path"])
    if not seen or seen[2] != entry["sha256"]:
        return False
    try:
        stat = (root / entry["path"]).stat()
    except FileNotFoundError:
        return False
    return [stat.st_size, stat.st_mtime_ns] == seen[:2]


def verify(dataset: Dataset, *, jobs: int = 8) -> Report:
    """Hash every file that changed since the last verification, and record the results."""
    files = dataset.files()
    record = load_record(dataset)
    report = Report(files=len(files), bytes=sum(entry["bytes"] for entry in files))
    pending = [entry for entry in files if not recorded(record, dataset.root, entry)]

    def check(entry: dict[str, Any]) -> tuple[dict[str, Any], list[Any] | None]:
        path = dataset.root / entry["path"]
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            return entry, None
        stat = path.stat()
        return entry, [stat.st_size, stat.st_mtime_ns, sha256_file(path)]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for entry, seen in pool.map(check, pending):
            if seen is not None:
                report.hashed += 1
            if seen is not None and seen[2] == entry["sha256"]:
                record[entry["path"]] = seen
            else:
                record.pop(entry["path"], None)
                report.bad.append(entry["path"])
    listed = {entry["path"] for entry in files} | {dataset.manifest}
    report.extra = sorted(
        relative
        for path in dataset.root.rglob("*")
        if path.is_file()
        and (relative := path.relative_to(dataset.root).as_posix()) not in listed
        and not path.name.startswith(".")
    )
    record = {path: seen for path, seen in record.items() if path in listed}
    save_record(dataset, record)
    return report


def require_verified(dataset: Dataset) -> list[dict[str, Any]]:
    """The dataset's files, if every one matches the verification record (sizes and mtimes; no hashing)."""
    files = dataset.files()
    record = load_record(dataset)
    changed = [entry["path"] for entry in files if not recorded(record, dataset.root, entry)]
    if changed:
        raise ExternalError(
            f"{dataset.id}: {len(changed)} of {len(files)} files are missing, changed or not verified yet "
            f"(first: {changed[0]}); run `demo.sh data fetch --verify-only`"
        )
    return files
