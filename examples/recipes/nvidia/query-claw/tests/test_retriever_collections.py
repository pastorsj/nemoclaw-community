# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    client = types.ModuleType("nemo_retriever")
    client.RetrieverServiceClient = object
    path = ROOT / "deploy" / "retriever_collections.py"
    spec = importlib.util.spec_from_file_location("query_claw_retriever", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("nemo_retriever")
    sys.modules["nemo_retriever"] = client
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("nemo_retriever", None)
        else:
            sys.modules["nemo_retriever"] = previous
    return module


RETRIEVER = load_module()


class ReviewedCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "documents").mkdir()
        self.first = self.root / "documents/first.md"
        self.second = self.root / "documents/second.md"
        self.first.write_text("first\n", encoding="utf-8")
        self.second.write_text("second\n", encoding="utf-8")
        self.manifest = {
            "schema_version": 2,
            "collection": "reviewed-v1",
            "document_count": 2,
            "documents": [
                {
                    "id": "first",
                    "file": "documents/first.md",
                    "sha256": hashlib.sha256(self.first.read_bytes()).hexdigest(),
                },
                {
                    "id": "second",
                    "file": "documents/second.md",
                    "sha256": hashlib.sha256(self.second.read_bytes()).hexdigest(),
                },
            ],
        }
        self.write_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def test_ingests_only_reviewed_manifest_documents(self) -> None:
        files = RETRIEVER._reviewed_corpus_files(self.root, "reviewed-v1")
        self.assertEqual([self.first.resolve(), self.second.resolve()], files)

    def test_rejects_checksum_and_undeclared_file_drift(self) -> None:
        self.first.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            RETRIEVER._reviewed_corpus_files(self.root, "reviewed-v1")

        self.first.write_text("first\n", encoding="utf-8")
        (self.root / "documents/extra.md").write_text("extra\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "differs from its manifest"):
            RETRIEVER._reviewed_corpus_files(self.root, "reviewed-v1")

    def test_rejects_collection_or_count_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "manifest contract"):
            RETRIEVER._reviewed_corpus_files(self.root, "other-v1")
        self.manifest["document_count"] = 1
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "manifest contract"):
            RETRIEVER._reviewed_corpus_files(self.root, "reviewed-v1")


class ActiveDatasetTests(unittest.TestCase):
    def test_reads_one_dataset_and_rejects_unknown_runtime_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            document = root / "documents/evidence.md"
            document.write_text("evidence\n", encoding="utf-8")
            manifest = root / "active-dataset.json"
            active = {
                "schema_version": 1,
                "id": "supply-chain",
                "title": "Synthetic Supply Chain",
                "fingerprint": "0" * 64,
                "database": {
                    "engine": "postgres-csv",
                    "name": "query_claw",
                    "path": "structured",
                },
                "documents": {
                    "path": "documents",
                    "collection": "query-claw-supply-chain",
                },
                "prediction": {"mode": "native"},
            }
            manifest.write_text(json.dumps(active), encoding="utf-8")

            datasets = RETRIEVER._active_document_sets(manifest)

            self.assertEqual(len(datasets), 1)
            self.assertEqual(datasets[0]["files"], [document.resolve()])
            active["database"]["engine"] = "unknown"
            manifest.write_text(json.dumps(active), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "database engine"):
                RETRIEVER._active_document_sets(manifest)


class ProvenanceTests(unittest.TestCase):
    def pipeline_configuration(self) -> dict:
        pipeline = {
            "extract_params": {"method": "pdfium", "api_key": "pipeline-secret"},
            "embed_params": {
                "model_name": "nvidia/nemotron-3-embed-1b",
                "embed_model_name": "nvidia/nemotron-3-embed-1b",
                "embed_model_provider_prefix": "nvidia",
                "embed_invoke_url": "https://private.example/embeddings",
            },
            "nim_endpoints": {
                "embed_model_name": "nvidia/nemotron-3-embed-1b",
                "embed_model_provider_prefix": "nvidia",
                "rerank_model_name": "nvidia/nvidia/llama-3.2-nv-rerankqa-1b-v2",
                "rerank_invoke_url": "https://private.example/rerank",
                "api_key": "endpoint-secret",
            },
        }
        return {
            "pipelines": {
                "realtime": copy.deepcopy(pipeline),
                "batch": copy.deepcopy(pipeline),
            },
            "allowed_overrides": {"secret": "policy-secret"},
        }

    def test_pipeline_identity_is_allowlisted_and_requires_matching_pools(self) -> None:
        configuration = self.pipeline_configuration()
        identity = RETRIEVER._safe_pipeline_identity(configuration)
        self.assertEqual(
            {
                "document_parser_method": "pdfium",
                "embedding_model": "nvidia/nemotron-3-embed-1b",
                "embedding_model_provider_prefix": "nvidia",
                "reranking_model": "nvidia/nvidia/llama-3.2-nv-rerankqa-1b-v2",
            },
            identity,
        )
        serialized = json.dumps(identity)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("secret", serialized)

        configuration["pipelines"]["batch"]["nim_endpoints"]["rerank_model_name"] = (
            "different-model"
        )
        with self.assertRaisesRegex(RuntimeError, "identities differ"):
            RETRIEVER._safe_pipeline_identity(configuration)

    def test_receipt_records_runtime_identity_without_service_secrets(self) -> None:
        receipt = RETRIEVER._provenance_receipt(
            expected_version="26.08.1",
            image_digest="sha256:" + "2" * 64,
            image_revision=None,
            openapi={
                "info": {"version": "26.08.1"},
                "servers": [{"url": "https://private.example/service-secret"}],
            },
            pipeline_configuration=self.pipeline_configuration(),
            ingestion_defaults={
                "input_modes": ["text"],
                "request_split_config": None,
                "text_parser": {
                    "method": "plain-text",
                    "encoding": "utf-8",
                    "decode_errors": "replace",
                },
                "text_chunker": {
                    "method": "token",
                    "max_tokens": 1024,
                    "overlap_tokens": 0,
                    "tokenizer_model": "nvidia/llama-nemotron-embed-vl-1b-v2",
                    "tokenizer_revision": "582e3bf72aee355e3c59ed89de53543c5b0657ee",
                },
            },
        )
        self.assertEqual("26.08.1", receipt["service"]["api_version"])
        self.assertEqual(["text"], receipt["ingestion"]["input_modes"])
        self.assertEqual(
            "nvidia/nemotron-3-embed-1b",
            receipt["ingestion"]["embedding"]["model"],
        )
        serialized = json.dumps(receipt)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("secret", serialized)

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            RETRIEVER._provenance_receipt(
                expected_version="26.08.1",
                image_digest="sha256:" + "2" * 64,
                image_revision=None,
                openapi={"info": {"version": "26.08.2"}},
                pipeline_configuration=self.pipeline_configuration(),
                ingestion_defaults={},
            )


if __name__ == "__main__":
    unittest.main()
