# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    path = EXAMPLE_ROOT / "scripts" / "generate_data.py"
    spec = importlib.util.spec_from_file_location("query_claw_generate_data", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _load_verifier():
    path = EXAMPLE_ROOT / "scripts" / "verify.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("query_claw_verify", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_retriever_registry(
    path: Path, deny_tools: list[str], *, pending: bool = False
) -> None:
    bridge: dict[str, object] = {"denyTools": deny_tools}
    if pending:
        bridge["pendingDenyTools"] = deny_tools
    path.write_text(
        json.dumps(
            {"sandboxes": {"query-claw": {"mcp": {"bridges": {"retriever": bridge}}}}}
        ),
        encoding="utf-8",
    )


class QueryClawDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="query-claw-test-")
        self.output = Path(self.temp_dir.name)
        self.manifest = GENERATOR.generate(GENERATOR.DEFAULT_SPEC, self.output)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_same_seed_produces_same_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="query-claw-test-second-"
        ) as second_dir:
            second = GENERATOR.generate(GENERATOR.DEFAULT_SPEC, Path(second_dir))
        self.assertEqual(self.manifest["fingerprint"], second["fingerprint"])

    def test_regeneration_preserves_service_directory_inodes(self) -> None:
        structured = self.output / "service" / "structured"
        documents = self.output / "service" / "documents"
        before = (structured.stat().st_ino, documents.stat().st_ino)
        GENERATOR.generate(GENERATOR.DEFAULT_SPEC, self.output)
        self.assertEqual(before, (structured.stat().st_ino, documents.stat().st_ino))

    def test_generated_pack_passes_integrity_validation(self) -> None:
        summary = GENERATOR.validate(self.output)
        self.assertEqual(1524, summary["purchase_orders"])
        self.assertEqual(24, summary["evaluation_orders"])
        self.assertEqual(10, summary["documents"])

    def test_service_data_contains_no_prediction_labels(self) -> None:
        service_files = {
            path.relative_to(self.output).as_posix()
            for path in (self.output / "service").rglob("*")
            if path.is_file()
        }
        self.assertNotIn("service/evaluation/labels.csv", service_files)
        orders = _rows(self.output / "service" / "structured" / "purchase_orders.csv")
        self.assertNotIn("delivered_date", orders[0])
        self.assertNotIn("delivery_outcome", orders[0])
        self.assertTrue({"split", "status_at_cutoff"} <= set(orders[0]))
        outcomes = _rows(
            self.output / "service" / "structured" / "delivery_outcomes.csv"
        )
        historical_ids = {
            row["order_id"] for row in orders if row["split"] == "history"
        }
        evaluation_ids = {
            row["order_id"] for row in orders if row["split"] == "evaluation"
        }
        self.assertEqual(1500, len(historical_ids))
        self.assertEqual(historical_ids, {row["order_id"] for row in outcomes})
        self.assertTrue(evaluation_ids.isdisjoint(row["order_id"] for row in outcomes))
        self.assertTrue(all(row["outcome"] in {"late", "on_time"} for row in outcomes))

    def test_service_events_stop_at_prediction_cutoff(self) -> None:
        cutoff = self.manifest["prediction_cutoff"]
        events = _rows(self.output / "service" / "structured" / "shipment_events.csv")
        outcomes = _rows(
            self.output / "service" / "structured" / "delivery_outcomes.csv"
        )
        self.assertTrue(all(row["event_date"] <= cutoff for row in events))
        self.assertTrue(all(row["outcome_date"] <= cutoff for row in outcomes))
        self.assertEqual(cutoff, max(row["outcome_date"] for row in outcomes))

    def test_evaluation_labels_match_the_fixed_prediction_window(self) -> None:
        labels = _rows(self.output / "evaluation" / "labels.csv")
        cutoff = self.manifest["prediction_cutoff"]
        horizon_end = self.manifest["as_of_date"]
        for row in labels:
            expected = str(
                row["late"] == "true"
                and cutoff < row["actual_delivery_date"] <= horizon_end
            ).lower()
            self.assertEqual(expected, row["late_within_horizon"])

    def test_primary_and_foreign_keys_resolve(self) -> None:
        structured = self.output / "service" / "structured"
        suppliers = {row["supplier_id"] for row in _rows(structured / "suppliers.csv")}
        facilities = {
            row["facility_id"] for row in _rows(structured / "facilities.csv")
        }
        products = {row["product_id"] for row in _rows(structured / "products.csv")}
        orders = _rows(structured / "purchase_orders.csv")
        self.assertEqual(len(orders), len({row["order_id"] for row in orders}))
        self.assertTrue(all(row["supplier_id"] in suppliers for row in orders))
        self.assertTrue(all(row["facility_id"] in facilities for row in orders))
        self.assertTrue(all(row["product_id"] in products for row in orders))


class QueryClawContractTests(unittest.TestCase):
    @staticmethod
    def _ready_retriever_status(
        tool_discovery: dict[str, object],
    ) -> dict[str, object]:
        return {
            "agent": "hermes",
            "support": {
                "supported": True,
                "mode": "bridge",
                "adapter": "hermes-config",
            },
            "provider": {
                "registryPresent": True,
                "gatewayPresent": True,
                "attached": True,
                "credentialReady": True,
            },
            "policy": {"registryPresent": True, "gatewayPresent": True},
            "adapter": {"registered": True},
            "trustedPrivateTarget": {
                "state": "match",
                "recordedPins": ["10.0.0.2"],
            },
            "toolDiscovery": tool_discovery,
        }

    def test_root_env_example_is_the_single_operator_template(self) -> None:
        template = (EXAMPLE_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertFalse((EXAMPLE_ROOT / "deploy" / ".env.example").exists())
        for name in (
            "GSF_SOURCE_DIR",
            "NVIDIA_INFERENCE_API_KEY",
            "KUMO_RFM_API_URL",
            "KUMO_RFM_API_KEY",
            "NEMOCLAW_SANDBOX_NAME",
            "NEMOCLAW_DASHBOARD_PORT",
            "NEMOCLAW_HERMES_API_PORT",
        ):
            self.assertEqual(1, template.count(f"\n{name}="), name)
        self.assertNotIn("\nGSF_SOURCE_REVISION=", template)
        common = (EXAMPLE_ROOT / "deploy/lib/common.sh").read_text(encoding="utf-8")
        self.assertRegex(
            common,
            r"(?m)^readonly QUERY_CLAW_GSF_COMMIT=[0-9a-f]{40}$",
        )
        self.assertRegex(
            common,
            r"(?m)^readonly QUERY_CLAW_RETRIEVER_COMMIT=[0-9a-f]{40}$",
        )
        self.assertNotIn("\nNEMO_RETRIEVER_IMAGE=", template)

    def test_contract_and_skill_tree_are_valid(self) -> None:
        contracts = VERIFIER.load_contracts()
        self.assertEqual({"ontology", "retriever"}, set(contracts["routes"]))
        self.assertEqual(
            {
                "gsf": {"auth": "oauth"},
                "retriever": {
                    "auth": "managed-bearer",
                    "token_env": "NEMO_RETRIEVER_API_TOKEN",
                },
            },
            contracts["registrations"],
        )
        skills = {
            path.parent.name for path in (EXAMPLE_ROOT / "skills").glob("*/SKILL.md")
        }
        self.assertEqual(
            {
                "query-claw",
                "query-claw-structured",
                "retriever-mcp",
                "query-claw-predictive",
            },
            skills,
        )
        self.assertTrue(
            (EXAMPLE_ROOT / "skills/query-claw/references/evidence.md").is_file()
        )
        coordinator = (
            EXAMPLE_ROOT / "skills/query-claw/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("still run each independent", coordinator)
        self.assertIn("pass its bare name exactly", coordinator)
        self.assertIn('A named "prediction anchor"', coordinator)
        self.assertIn("is a historical\n  cutoff", coordinator)
        self.assertIn("do not test\n  a target by attempting a prediction", coordinator)
        self.assertIn("observed historical records only", coordinator)
        predictive = (
            EXAMPLE_ROOT / "skills/query-claw-predictive/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`prediction: true`", predictive)
        self.assertIn("Treat a returned service or PQL error", predictive)
        self.assertIn("follow\n   `query-claw-structured` once", predictive)
        self.assertIn("finite numeric score", predictive)
        self.assertIn("attempted but unavailable", predictive)
        self.assertIn("a prediction anchor alone is historical", predictive)
        self.assertIn("capability or\n   evidence-scope question", predictive)
        self.assertIn("call no data tool if any is missing", predictive)
        self.assertIn("separate, explicitly records-only", predictive)
        structured = (
            EXAMPLE_ROOT / "skills/query-claw-structured/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("historical facts through or as of an anchor", structured)
        self.assertIn("never multiply an amount through join", structured)
        self.assertIn("observed\n   historical records only", structured)
        self.assertIn("response took the wrong route", structured)
        self.assertIn("Discard its rows", structured)
        self.assertIn("never use those rows as historical evidence", structured)
        gsf_setup = (EXAMPLE_ROOT / "deploy/setup-gsf.sh").read_text(encoding="utf-8")
        self.assertIn("delete_all_data()", gsf_setup)
        self.assertNotIn('delete_semantic_layer("query_claw")', gsf_setup)
        compose = (EXAMPLE_ROOT / "deploy/compose.override.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('GSF_MCP_CHAT_TIMEOUT_S: "840"', compose)
        self.assertIn("KUMO_RFM_API_URL: ${QUERY_CLAW_KUMO_RFM_API_URL}", compose)
        self.assertIn("KUMO_RFM_API_KEY: ${QUERY_CLAW_KUMO_RFM_API_KEY}", compose)
        self.assertIn(
            "KUMO_GRAPH_CONTRACTS_FILE: ${QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE}",
            compose,
        )
        self.assertIn(
            "query-claw-mcp-root.crt:/etc/ssl/certs/query-claw-mcp-root.crt:ro",
            compose,
        )
        self.assertNotIn("- caddy_data:/data:ro", compose)
        self.assertIn("NVIDIA_RERANK_INVOKE_URL", compose)
        retriever_config = (EXAMPLE_ROOT / "deploy/retriever-service.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("mcp:\n  enabled: true", retriever_config)
        self.assertIn("rerank_model_name: ${NVIDIA_RERANK_MODEL}", retriever_config)
        retriever_setup = (
            EXAMPLE_ROOT / "deploy/retriever_collections.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"rerank": True', retriever_setup)
        self.assertIn('"collection_name": collection', retriever_setup)
        caddy = (EXAMPLE_ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        self.assertIn("reverse_proxy retriever:7670", caddy)
        self.assertNotIn("rewrite * /mcp\n", caddy)
        self.assertIn("redir /mcp/ 308", caddy)
        VERIFIER.validate_skill()

    def test_exact_native_retriever_inventory_is_accepted(self) -> None:
        self.assertEqual(
            [tool for tool in VERIFIER.NATIVE_RETRIEVER_TOOLS if tool != "query"],
            VERIFIER.RETRIEVER_DENY_TOOLS,
        )
        VERIFIER.check_inventory(
            "retriever", VERIFIER.NATIVE_RETRIEVER_TOOLS, VERIFIER.NATIVE_RETRIEVER_TOOLS
        )

    def test_native_denied_tool_intent_fails_closed_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "sandboxes.json"
            _write_retriever_registry(registry, VERIFIER.RETRIEVER_DENY_TOOLS)
            VERIFIER.check_retriever_deny_intent("query-claw", {}, registry)

            _write_retriever_registry(registry, ["answer"])
            with self.assertRaisesRegex(ValueError, "durable denied tools"):
                VERIFIER.check_retriever_deny_intent("query-claw", {}, registry)

            _write_retriever_registry(
                registry, VERIFIER.RETRIEVER_DENY_TOOLS, pending=True
            )
            with self.assertRaisesRegex(ValueError, "incomplete denied-tool update"):
                VERIFIER.check_retriever_deny_intent("query-claw", {}, registry)

            _write_retriever_registry(registry, VERIFIER.RETRIEVER_DENY_TOOLS)
            with self.assertRaisesRegex(ValueError, "reported denied tools"):
                VERIFIER.check_retriever_deny_intent(
                    "query-claw", {"denyTools": ["answer"]}, registry
                )

    def test_missing_or_extra_tools_fail_closed(self) -> None:
        tools = VERIFIER.NATIVE_RETRIEVER_TOOLS
        with self.assertRaisesRegex(ValueError, "missing required tools"):
            VERIFIER.check_inventory("retriever", tools, [])
        with self.assertRaisesRegex(ValueError, "outside its allowed contract"):
            VERIFIER.check_inventory(
                "retriever", tools, tools + ["delete_collection"]
            )

    def test_truncated_or_inconsistent_discovery_fails_closed(self) -> None:
        complete = {"ok": True, "count": 1, "tools": ["query"], "truncated": False}
        self.assertEqual(["query"], VERIFIER.discovery_tools("retriever", complete))
        with self.assertRaisesRegex(ValueError, "truncated"):
            VERIFIER.discovery_tools("retriever", complete | {"truncated": True})
        with self.assertRaisesRegex(ValueError, "count"):
            VERIFIER.discovery_tools("retriever", complete | {"count": 2})

    def test_native_private_bridge_accepts_only_the_release_discovery_limit(self) -> None:
        ready = {
            "agent": "hermes",
            "support": {
                "supported": True,
                "mode": "bridge",
                "adapter": "hermes-config",
            },
            "provider": {
                "registryPresent": True,
                "gatewayPresent": True,
                "attached": True,
                "credentialReady": True,
            },
            "policy": {"registryPresent": True, "gatewayPresent": True},
            "adapter": {"registered": True},
            "trustedPrivateTarget": {
                "state": "match",
                "recordedPins": ["10.0.0.2"],
            },
            "toolDiscovery": {
                "ok": False,
                "count": 0,
                "tools": [],
                "truncated": False,
                "detail": VERIFIER.PRIVATE_DISCOVERY_LIMIT,
            },
        }
        self.assertIsNone(VERIFIER.managed_bridge_tools("retriever", ready))
        with self.assertRaisesRegex(ValueError, "tool discovery did not succeed"):
            VERIFIER.managed_bridge_tools(
                "retriever",
                ready
                | {
                    "toolDiscovery": ready["toolDiscovery"]
                    | {"detail": "sandbox unreachable"}
                },
            )
        with self.assertRaisesRegex(ValueError, "private bridge is not ready"):
            VERIFIER.managed_bridge_tools(
                "retriever", ready | {"adapter": {"registered": False}}
            )

    def test_live_verify_accepts_valid_private_bridge_status_on_nonzero_exit(
        self,
    ) -> None:
        status = self._ready_retriever_status(
            {
                "ok": False,
                "count": 0,
                "tools": [],
                "truncated": False,
                "detail": VERIFIER.PRIVATE_DISCOVERY_LIMIT,
            }
        )
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(status), stderr="probe unavailable"
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "sandboxes.json"
            _write_retriever_registry(registry, VERIFIER.RETRIEVER_DENY_TOOLS)
            with (
                patch.object(VERIFIER.subprocess, "run", return_value=result) as run,
                patch.object(VERIFIER.time, "sleep") as sleep,
                patch.dict(VERIFIER.os.environ, {"QUERY_CLAW_ENABLE_RETRIEVER": "1"}),
            ):
                VERIFIER.live_verify(
                    "query-claw",
                    "nemohermes",
                    VERIFIER.DEFAULT_CONTRACTS,
                    registry,
                )

            run.assert_called_once()
            sleep.assert_not_called()

if __name__ == "__main__":
    unittest.main()
