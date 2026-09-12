#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ingest or verify active Query Claw document collections."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from nemo_retriever import RetrieverServiceClient


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_LEGACY_AIQ_FACTORY_COLLECTION = (
    "aiq-booth-synthetic-ai-factory-nemotron3-embed-2048-v1"
)


def _supported_corpus_manifest(manifest: dict[str, Any], collection: str) -> bool:
    if manifest.get("schema_version") == 2:
        return manifest.get("collection") == collection
    source = manifest.get("source_dataset")
    return (
        manifest.get("schema_version") == 1
        and collection == _LEGACY_AIQ_FACTORY_COLLECTION
        and manifest.get("collection") == _LEGACY_AIQ_FACTORY_COLLECTION
        and manifest.get("synthetic") is True
        and manifest.get("review")
        == {"policy": "grounding-and-booth-copy-v1", "status": "reviewed"}
        and isinstance(source, dict)
        and source.get("id") == "synthetic-ai-factory"
    )


def _reviewed_corpus_files(path: Path, collection: str) -> list[Path]:
    """Return exactly the documents declared by an AIQ3 corpus manifest."""

    path = path.resolve(strict=True)
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        if path.is_symlink() or any(candidate.is_symlink() for candidate in candidates):
            raise RuntimeError("active document views must not contain symlinks")
        selected = [candidate for candidate in candidates if candidate.is_file()]
        if not selected:
            raise RuntimeError("active dataset has no documents")
        return selected

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("reviewed document manifest is invalid") from error
    documents = manifest.get("documents")
    if (
        not _supported_corpus_manifest(manifest, collection)
        or not isinstance(documents, list)
        or manifest.get("document_count") != len(documents)
        or not documents
    ):
        raise RuntimeError("reviewed document manifest contract is invalid")

    selected: list[Path] = []
    identifiers: set[str] = set()
    relative_files: set[str] = set()
    for item in documents:
        if not isinstance(item, dict):
            raise RuntimeError("reviewed document manifest entry is invalid")
        identifier = item.get("id")
        relative = item.get("file")
        digest = item.get("sha256")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or not isinstance(relative, str)
            or not relative
            or relative in relative_files
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise RuntimeError("reviewed document manifest entry is invalid")
        candidate = path / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(path)
        except (OSError, ValueError) as error:
            raise RuntimeError("reviewed document path escapes its corpus") from error
        if candidate.is_symlink() or not resolved.is_file():
            raise RuntimeError("reviewed document must be a regular file")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError("reviewed document checksum differs from its manifest")
        identifiers.add(identifier)
        relative_files.add(relative)
        selected.append(resolved)

    corpus_files = {
        candidate.relative_to(path).as_posix()
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate != manifest_path
    }
    if corpus_files != relative_files:
        raise RuntimeError("reviewed document corpus differs from its manifest")
    return selected


def _active_document_sets(manifest_path: Path) -> list[dict[str, Any]]:
    root = manifest_path.parent.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("active dataset manifest is invalid")
    database = manifest.get("database")
    if database is not None and (
        not isinstance(database, dict)
        or database.get("engine") not in {"duckdb", "postgres-csv"}
    ):
        raise RuntimeError("active dataset database engine is invalid")
    prediction = manifest.get("prediction")
    if prediction is not None and (
        not isinstance(prediction, dict)
        or prediction.get("mode") not in {"native", "reviewed"}
    ):
        raise RuntimeError("active dataset prediction mode is invalid")

    def files(view: str, collection: str) -> list[Path]:
        path = (root / view).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise RuntimeError("document view escapes the active data tree") from error
        return _reviewed_corpus_files(path, collection)

    documents = manifest.get("documents")
    if not isinstance(documents, dict):
        raise RuntimeError("active dataset has no document contract")
    view = documents.get("path")
    collection = documents.get("collection")
    if not isinstance(view, str) or not isinstance(collection, str):
        raise RuntimeError("document path and Retriever collection must be paired")
    return [
        {
            "id": manifest["id"],
            "title": manifest["title"],
            "collection": collection,
            "files": files(view, collection),
            "fingerprint": manifest["fingerprint"],
        }
    ]


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


def _qualified_query(token: str, title: str, collection: str) -> list[dict[str, Any]]:
    request = Request(
        "http://127.0.0.1:7670/v1/query",
        data=json.dumps(
            {
                "query": f"What information is available about {title}?",
                "collection_name": collection,
                "top_k": 1,
                "format": "hits",
                "rerank": True,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-NRL-Scope": "query-claw",
        },
        method="POST",
    )
    with urlopen(request, timeout=300) as response:  # noqa: S310 - loopback only
        result = json.load(response)
    try:
        hits = result["results"][0]["hits"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(
            "Retriever reranked query returned an invalid response"
        ) from error
    if not isinstance(hits, list):
        raise RuntimeError("Retriever reranked query returned an invalid hit list")
    if hits and (
        not isinstance(hits[0], dict)
        or isinstance(hits[0].get("_rerank_score"), bool)
        or not isinstance(hits[0].get("_rerank_score"), (int, float))
    ):
        raise RuntimeError("Retriever qualification did not observe a rerank score")
    return hits


def _service_json(token: str, path: str) -> dict[str, Any]:
    request = Request(
        f"http://127.0.0.1:7670{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-NRL-Scope": "query-claw",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback only
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError(f"Retriever {path} returned an invalid response")
    return result


def _pipeline_identity(pipeline: Any) -> dict[str, Any]:
    try:
        identity = {
            "document_parser_method": pipeline["extract_params"]["method"],
            "embedding_model": pipeline["embed_params"]["model_name"],
            "embedding_model_provider_prefix": pipeline["embed_params"].get(
                "embed_model_provider_prefix"
            ),
            "reranking_model": pipeline["nim_endpoints"]["rerank_model_name"],
        }
    except (KeyError, TypeError) as error:
        raise RuntimeError("Retriever pipeline configuration is incomplete") from error
    required = {
        key: value
        for key, value in identity.items()
        if key != "embedding_model_provider_prefix"
    }
    prefix = identity["embedding_model_provider_prefix"]
    if not all(isinstance(value, str) and value for value in required.values()) or (
        prefix is not None and (not isinstance(prefix, str) or not prefix)
    ):
        raise RuntimeError("Retriever pipeline identity is invalid")
    return identity


def _safe_pipeline_identity(configuration: dict[str, Any]) -> dict[str, Any]:
    pipelines = configuration.get("pipelines")
    if not isinstance(pipelines, dict):
        raise RuntimeError("Retriever pipeline configuration is invalid")
    realtime = _pipeline_identity(pipelines.get("realtime"))
    batch = _pipeline_identity(pipelines.get("batch"))
    if realtime != batch:
        raise RuntimeError("Retriever realtime and batch pipeline identities differ")
    return realtime


def _official_ingestion_defaults(datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """Read parsing and chunking defaults from the installed official runtime."""

    from nemo_retriever.common.modality.txt.split import (
        DEFAULT_TOKENIZER_MODEL_ID,
    )
    from nemo_retriever.common.params import TextChunkParams
    from nemo_retriever.models.hf_model_registry import get_hf_revision
    from nemo_retriever.service.utils.file_type import (
        infer_extraction_mode_from_filename,
    )

    input_modes: set[str] = set()
    for dataset in datasets:
        for path in dataset["files"]:
            mode = infer_extraction_mode_from_filename(path.name)
            if mode is None:
                raise RuntimeError(
                    f"Retriever does not recognize document type {path.suffix!r}"
                )
            input_modes.add(mode)

    chunk = TextChunkParams()
    tokenizer_model = chunk.tokenizer_model_id or DEFAULT_TOKENIZER_MODEL_ID
    return {
        "input_modes": sorted(input_modes),
        "request_split_config": None,
        "text_parser": {
            "method": "plain-text",
            "encoding": chunk.encoding,
            "decode_errors": "replace",
        },
        "text_chunker": {
            "method": "token",
            "max_tokens": chunk.max_tokens,
            "overlap_tokens": chunk.overlap_tokens,
            "tokenizer_model": tokenizer_model,
            "tokenizer_revision": get_hf_revision(tokenizer_model),
        },
    }


def _provenance_receipt(
    *,
    expected_version: str,
    image_digest: str | None,
    image_revision: str | None,
    openapi: dict[str, Any],
    pipeline_configuration: dict[str, Any],
    ingestion_defaults: dict[str, Any],
) -> dict[str, Any]:
    info = openapi.get("info")
    api_version = info.get("version") if isinstance(info, dict) else None
    if api_version != expected_version:
        raise RuntimeError(
            f"Retriever API version {api_version!r} does not match {expected_version!r}"
        )
    if image_digest is not None and not _SHA256_RE.fullmatch(image_digest):
        raise RuntimeError("Retriever image digest is invalid")
    if image_revision is not None and not _REVISION_RE.fullmatch(image_revision):
        raise RuntimeError("Retriever image revision is invalid")
    if image_digest is None and image_revision is None:
        raise RuntimeError("Retriever image digest or source revision is required")

    pipeline = _safe_pipeline_identity(pipeline_configuration)
    return {
        "schema_version": 1,
        "kind": "query-claw-nemo-retriever-provenance",
        "service": {
            "api_version": api_version,
            "image_digest": image_digest,
            "source_revision": image_revision,
        },
        "ingestion": {
            **ingestion_defaults,
            "document_parser": {
                "method": pipeline["document_parser_method"],
                "applies_to": "document inputs",
            },
            "embedding": {
                "model": pipeline["embedding_model"],
                "provider_prefix": pipeline["embedding_model_provider_prefix"],
            },
        },
        "retrieval": {"reranking_model": pipeline["reranking_model"]},
    }


def _capture_provenance(
    token: str,
    datasets: list[dict[str, Any]],
    *,
    expected_version: str,
    image_digest: str | None,
    image_revision: str | None,
) -> dict[str, Any]:
    return _provenance_receipt(
        expected_version=expected_version,
        image_digest=image_digest,
        image_revision=image_revision,
        openapi=_service_json(token, "/openapi.json"),
        pipeline_configuration=_service_json(token, "/v1/ingest/pipeline-config"),
        ingestion_defaults=_official_ingestion_defaults(datasets),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ingest", "verify", "provenance"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/query-claw-active/active-dataset.json"),
    )
    parser.add_argument("--expected-version")
    parser.add_argument("--image-digest")
    parser.add_argument("--image-revision")
    args = parser.parse_args()

    token = os.environ.get("NEMO_RETRIEVER_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("NEMO_RETRIEVER_API_TOKEN must contain 32 characters")
    datasets = _active_document_sets(args.manifest)
    if not datasets:
        if args.action == "provenance":
            raise RuntimeError("cannot attest an empty Retriever corpus")
        print("No active document views")
        return 0

    if args.action == "provenance":
        if not args.expected_version:
            raise RuntimeError("Retriever provenance requires the expected version")
        print(
            json.dumps(
                _capture_provenance(
                    token,
                    datasets,
                    expected_version=args.expected_version,
                    image_digest=args.image_digest,
                    image_revision=args.image_revision,
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    client = RetrieverServiceClient(
        base_url="http://127.0.0.1:7670",
        api_token=token,
        scope="query-claw",
        max_concurrency=2,
    )
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

        hits = _qualified_query(token, dataset["title"], dataset["collection"])
        if not hits:
            raise RuntimeError(
                f"Retriever collection {dataset['collection']} returned no evidence"
            )
        print(f"ready: {dataset['id']} -> {dataset['collection']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
