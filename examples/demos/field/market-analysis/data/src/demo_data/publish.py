# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Builds cached by content digest, published through one symlink.

    DATA_DIR/
      active -> builds/<pack>@<version>+<profile>+<digest12>     switched atomically; services read only this
      builds/<name>/pack.json                                     the resolved pack plus what each part produced
      builds/<name>/{tables,structured,ontology,prediction}/      the structured part
      builds/<name>/corpus/documents.jsonl                        the corpus part
      downloads/sha256/<digest>                                   pinned public files, shared by every build
      cache/{rollups,generated}/<key>/, cache/sec/<date>/         daily rollups, generated raw datasets, SEC snapshots

A build is keyed by everything it depends on, so preparing an unchanged pack is a no-op and changing any input
starts a new build. Each part is written to a staging directory and moved into place before pack.json records
it; a build becomes active only once its structured part (if the pack has one) is recorded.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from demo_data.corpus import document_errors
from demo_data.corpus.common import sha256_file

PARTS = {"structured": ("tables", "structured", "ontology", "prediction"), "corpus": ("corpus",)}


def builder_fingerprint() -> str:
    """Identifies the builder: its source, its schemas and the DuckDB it writes with."""
    package = Path(__file__).resolve().parent
    sha = hashlib.sha256(f"duckdb {duckdb.__version__}".encode())
    for file in sorted([*package.rglob("*.py"), *(package.parents[1] / "schemas").glob("*.json")]):
        sha.update(file.read_bytes())
    return sha.hexdigest()


@contextmanager
def locked(data_dir: Path) -> Iterator[None]:
    """Serialize builders sharing a data directory (for example two one-shot containers on one volume)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Build:
    """One build directory and its pack.json."""

    def __init__(self, data_dir: Path, name: str) -> None:
        self.directory = data_dir.resolve() / "builds" / name
        manifest = self.directory / "pack.json"
        self.manifest: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}

    @property
    def name(self) -> str:
        return self.directory.name

    def has(self, part: str) -> bool:
        return part in self.manifest.get("parts", {})

    @contextmanager
    def staging(self, part: str) -> Iterator[Path]:
        """A scratch directory for one part; on success its contents replace the part in the build."""
        staging = self.directory.parent / f".staging-{part}-{self.name}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            yield staging
            self.directory.mkdir(exist_ok=True)
            for child in staging.iterdir():
                shutil.rmtree(self.directory / child.name, ignore_errors=True)
                child.rename(self.directory / child.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def record(self, part: str, resolved: dict[str, Any], receipt: dict[str, Any]) -> None:
        """Rewrite pack.json with the resolved pack and this part's receipt and file digests."""
        files = {
            file.relative_to(self.directory).as_posix(): sha256_file(file)
            for directory in PARTS[part]
            for file in sorted((self.directory / directory).rglob("*"))
            if file.is_file()
        }
        parts = self.manifest.get("parts", {}) | {part: receipt | {"files": files}}
        self.manifest = {"schema_version": "1", "build": self.name, **resolved, "parts": parts}
        write_json(self.directory / "pack.json", self.manifest)

    def ready(self) -> bool:
        """Recorded, with its structured part when the pack has one."""
        return bool(self.manifest) and (self.has("structured") or "structured" not in self.manifest)


def activate(data_dir: Path, build: Build) -> None:
    """Point DATA_DIR/active at the build with an atomic rename, so readers never see a partial switch."""
    temporary = data_dir / f".active-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(Path("builds") / build.name)
    os.replace(temporary, data_dir / "active")


def active_build(data_dir: Path) -> Path | None:
    link = data_dir / "active"
    return link.resolve() if link.is_symlink() and link.resolve().is_dir() else None


def verify(directory: Path) -> list[str]:
    """Check a build against its pack.json: file digests, database row counts and document rows."""
    manifest_path = directory / "pack.json"
    if not manifest_path.is_file():
        return [f"{directory} has no pack.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    for receipt in manifest["parts"].values():
        for relative, digest in receipt["files"].items():
            path = directory / relative
            if not path.is_file():
                errors.append(f"{relative} is missing")
            elif sha256_file(path) != digest:
                errors.append(f"{relative} changed after it was built")
    if errors:
        return errors
    if "structured" in manifest["parts"]:
        rows = manifest["parts"]["structured"]["rows"]
        with duckdb.connect(str(directory / manifest["structured"]["database"]), read_only=True) as connection:
            query = "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
            tables = {name for (name,) in connection.execute(query).fetchall()}
            for table in sorted(tables & set(rows)):
                (count,) = connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()
                if count != rows[table]:
                    errors.append(f"database table {table} has {count} rows, pack.json records {rows[table]}")
    if "corpus" in manifest["parts"]:
        path = directory / manifest["documents"]["path"]
        with path.open(encoding="utf-8") as stream:
            errors += document_errors([json.loads(line) for line in stream])
    return errors


def clean(data_dir: Path, *, downloads: bool = False) -> list[Path]:
    """Remove every build but the active one, leftover staging, and the cached rollups and generated datasets that
    no remaining build uses.

    With `downloads`, also remove the download cache and every other cache (rollups, SEC snapshots).
    """
    active = active_build(data_dir)
    builds = data_dir / "builds"
    removed = [path for path in sorted(builds.iterdir()) if path.resolve() != active] if builds.is_dir() else []
    for path in removed:
        shutil.rmtree(path)
    if downloads:
        stale = [path for path in (data_dir / "downloads", data_dir / "cache") if path.is_dir()]
    else:
        used: set[str] = set()
        for manifest in builds.glob("*/pack.json") if builds.is_dir() else []:
            pack = json.loads(manifest.read_text(encoding="utf-8"))
            used.add(pack.get("parts", {}).get("structured", {}).get("import", {}).get("rollup", {}).get("key"))
            used.add(Path(pack.get("market", {}).get("bars", {}).get("root", "/")).name)
        cached = [data_dir / "cache" / "rollups", data_dir / "cache" / "generated"]
        stale = [
            path for cache in cached if cache.is_dir() for path in sorted(cache.iterdir()) if path.name not in used
        ]
    for path in stale:
        shutil.rmtree(path)
    return removed + stale


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically: readers see the old file or the new one, never half of one."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
