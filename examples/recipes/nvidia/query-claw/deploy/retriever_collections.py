#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ingest or verify active Query Claw document collections."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from nemo_retriever import RetrieverServiceClient


def _active_document_sets(manifest_path: Path) -> list[dict[str, Any]]:
    root = manifest_path.parent.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(
        manifest.get("datasets"), list
    ):
        raise RuntimeError("active data-pack manifest is invalid")

    result = []
    for dataset in manifest["datasets"]:
        view = dataset.get("views", {}).get("documents")
        collection = dataset.get("bindings", {}).get("retriever", {}).get("collection")
        if view is None and collection is None:
            continue
        if not isinstance(view, str) or not isinstance(collection, str):
            raise RuntimeError("document view and Retriever collection must be paired")
        path = (root / view).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise RuntimeError("document view escapes the active data tree") from error
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        if path.is_symlink() or any(candidate.is_symlink() for candidate in candidates):
            raise RuntimeError("active document views must not contain symlinks")
        files = [candidate for candidate in candidates if candidate.is_file()]
        if not files:
            raise RuntimeError(f"dataset {dataset.get('id')} has no documents")
        result.append(
            {
                "id": dataset["id"],
                "title": dataset["title"],
                "collection": collection,
                "files": files,
                "fingerprint": manifest["fingerprint"],
            }
        )
    return result


def _wait(client: RetrieverServiceClient, job_id: str) -> None:
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if job.status == "completed":
            return
        if job.status in {"failed", "partial_success", "cancelled"}:
            raise RuntimeError(f"Retriever ingestion ended with status {job.status}")
        time.sleep(2)
    raise RuntimeError("Retriever ingestion did not finish within 30 minutes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ingest", "verify"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/query-claw-active/active-data-packs.json"),
    )
    args = parser.parse_args()

    token = os.environ.get("NEMO_RETRIEVER_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("NEMO_RETRIEVER_API_TOKEN must contain 32 characters")
    client = RetrieverServiceClient(
        base_url="http://127.0.0.1:7670",
        api_token=token,
        scope="query-claw",
        max_concurrency=2,
    )
    datasets = _active_document_sets(args.manifest)
    if not datasets:
        print("No active document views")
        return 0

    for dataset in datasets:
        if args.action == "ingest":
            client.create_collection(
                dataset["collection"],
                description=f"Query Claw: {dataset['title']}",
                metadata={"dataset_id": dataset["id"], "owner": "query-claw"},
            )
            key = hashlib.sha256(
                f"{dataset['fingerprint']}\0{dataset['id']}".encode()
            ).hexdigest()
            job = client.submit_documents(
                dataset["collection"], dataset["files"], idempotency_key=key
            )
            _wait(client, job.job_id)

        hits = client.query(
            f"What information is available about {dataset['title']}?",
            collection_name=dataset["collection"],
            top_k=1,
        )
        if not hits:
            raise RuntimeError(
                f"Retriever collection {dataset['collection']} returned no evidence"
            )
        print(f"ready: {dataset['id']} -> {dataset['collection']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
