# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fetch an external dataset from its source into $DATA_SOURCE_DIR/<id>/, checking every file as it lands.

A source is an fsspec URL, so one loop serves every kind:

    /mnt/datasets/minute-bars or file:///mnt/...   a local directory
    https://example.com/minute-bars/                a prefix: each file is fetched as <prefix><path>
    https://.../minute-bars.tar.gz                  one .tar or .tar.gz archive with the dataset at its top level
    s3://bucket/prefix, gs://bucket/prefix         object stores (credentials from the usual environment variables)
    hf://datasets/<owner>/<name>@<revision>/<prefix>    a Hugging Face dataset (HF_TOKEN)

The remote schemes need the `remote` extra, which the image installs. rsync is not a scheme: `demo.sh data fetch`
runs it on the host, which holds the SSH keys, and then verifies here.

The manifest comes first, and nothing else is fetched unless its fingerprint matches the pin. Each missing or
changed file is streamed to a hidden .part file while it is hashed, and renamed into place only if the hash
matches. Files already verified are skipped, so an interrupted fetch resumes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fsspec

from demo_data import external
from demo_data.external import Dataset
from demo_data.external import ExternalError
from demo_data.external import Report

ARCHIVES = (".tar", ".tar.gz", ".tgz")
BLOCK = 4 << 20


def fetch(dataset: Dataset, source: str, *, jobs: int = 8, attempts: int = 3) -> Report:
    """Bring `dataset.root` in line with the manifest at `source`, then verify it."""
    dataset.root.mkdir(parents=True, exist_ok=True)
    if source.split("?", 1)[0].endswith(ARCHIVES):
        unpack(dataset, source)
        return external.verify(dataset, jobs=jobs)
    fs, base = fsspec.core.url_to_fs(source, **storage_options(source))
    manifest = fs.cat_file(join(base, dataset.manifest))
    files = external.parse_manifest(manifest, dataset)
    write_atomically(dataset.manifest_path, manifest)
    record = external.load_record(dataset)
    pending = [entry for entry in files if not external.recorded(record, dataset.root, entry)]

    def download(entry: dict[str, Any]) -> tuple[str, list[Any] | Exception]:
        """The file's record, or the error of its last attempt."""
        target = dataset.root / entry["path"]
        for attempt in range(1, attempts + 1):
            try:
                return entry["path"], copy(fs, join(base, entry["path"]), target, entry["sha256"])
            except Exception as error:  # each backend raises its own network errors
                if attempt == attempts:
                    return entry["path"], error
                time.sleep(2**attempt)
        return entry["path"], ExternalError("no attempts")

    # A file that fails does not stop the others, and everything verified is recorded, so a rerun fetches only
    # what is still missing.
    failed = []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for count, (path, seen) in enumerate(pool.map(download, pending), start=1):
                if isinstance(seen, Exception):
                    failed.append(f"{path}: {seen}")
                else:
                    record[path] = seen
                if count % 100 == 0:  # keep the record current, even if the process is killed
                    external.save_record(dataset, record)
    finally:
        external.save_record(dataset, record)
    if failed:
        raise ExternalError(
            f"{dataset.id}: {len(failed):,} of {len(pending):,} files failed; a rerun retries only those. "
            f"First: {failed[0]}"
        )
    return external.verify(dataset, jobs=jobs)


def copy(fs: fsspec.AbstractFileSystem, url: str, target: Path, sha256: str) -> list[Any]:
    """Stream one file to a hidden .part file, hashing it; rename it into place only if the hash matches."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.part")
    digest = hashlib.sha256()
    try:
        with fs.open(url, "rb") as source, part.open("wb") as sink:
            while block := source.read(BLOCK):
                digest.update(block)
                sink.write(block)
        if digest.hexdigest() != sha256:
            raise ExternalError(f"{url} has sha256 {digest.hexdigest()}; the manifest says {sha256}")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    stat = target.stat()
    return [stat.st_size, stat.st_mtime_ns, sha256]


def unpack(dataset: Dataset, source: str) -> None:
    """Stream an archive into a staging directory, check its manifest, then move its files into the dataset."""
    staging = dataset.root.parent / f".{dataset.id}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with fsspec.open(source, "rb", **storage_options(source)) as stream:
            with tarfile.open(fileobj=stream, mode="r|*") as tar:
                tar.extractall(staging, filter="data")  # the data filter refuses unsafe paths and links
        manifest = staging / dataset.manifest
        if not manifest.is_file():
            raise ExternalError(f"{dataset.id}: the archive has no {dataset.manifest} at its top level")
        files = external.parse_manifest(manifest.read_bytes(), dataset)
        for entry in [*files, {"path": dataset.manifest}]:
            source_path, target = staging / entry["path"], dataset.root / entry["path"]
            if source_path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source_path, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def storage_options(url: str) -> dict[str, Any]:
    """Settings a scheme needs beyond what its library reads from the environment itself."""
    scheme = url.split("://", 1)[0] if "://" in url else "file"
    if scheme in ("http", "https"):
        # Stream each file in one GET. fsspec's default reads in Range requests, which some servers refuse.
        options: dict[str, Any] = {"block_size": 0}
        if token := os.environ.get("DATA_SOURCE_HTTP_TOKEN"):
            options["headers"] = {"Authorization": f"Bearer {token}"}
        return options
    if scheme == "s3" and (endpoint := os.environ.get("AWS_ENDPOINT_URL")):
        return {"endpoint_url": endpoint}
    if scheme in ("gs", "gcs") and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return {"token": "anon"}  # a public bucket
    return {}


def join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path}"


def write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)
