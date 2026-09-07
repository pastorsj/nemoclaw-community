# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import csv
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


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


def _load_mcp_adapter():
    class FakeFastMCP:
        def __init__(self, **_kwargs):
            self.tools = {}
            self.routes = {}

        def custom_route(self, path, **_kwargs):
            def register(function):
                self.routes[path] = function
                return function

            return register

        def tool(self, **_kwargs):
            def register(function):
                self.tools[function.__name__] = function
                return function

            return register

    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = MagicMock
    httpx.Client = MagicMock
    httpx.HTTPError = type("HTTPError", (Exception,), {})
    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = FakeFastMCP
    pydantic = types.ModuleType("pydantic")
    pydantic.Field = lambda **kwargs: kwargs
    starlette = types.ModuleType("starlette")
    starlette.__path__ = []
    responses = types.ModuleType("starlette.responses")
    responses.JSONResponse = lambda content, **kwargs: {"content": content, **kwargs}
    exceptions = types.ModuleType("fastmcp.exceptions")
    exceptions.ToolError = type("ToolError", (Exception,), {})
    auth = types.ModuleType("fastmcp.server.auth.auth")
    auth.AccessToken = type("AccessToken", (), {})
    auth.TokenVerifier = type("TokenVerifier", (), {"__init__": lambda self: None})
    modules = {
        "httpx": httpx,
        "fastmcp": fastmcp,
        "fastmcp.exceptions": exceptions,
        "fastmcp.server.auth.auth": auth,
        "pydantic": pydantic,
        "starlette": starlette,
        "starlette.responses": responses,
    }
    path = EXAMPLE_ROOT / "deploy" / "services" / "mcp-adapters" / "app.py"
    spec = importlib.util.spec_from_file_location("query_claw_mcp_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


MCP_ADAPTER = _load_mcp_adapter()


def _load_live_evaluator():
    path = EXAMPLE_ROOT / "scripts" / "evaluate_live.py"
    spec = importlib.util.spec_from_file_location("query_claw_evaluate_live", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LIVE_EVALUATOR = _load_live_evaluator()
SMOKE_CASES = dict(
    LIVE_EVALUATOR.load_suite(EXAMPLE_ROOT / "evaluations" / "smoke.json")[1]
)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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

    def test_smoke_answers_match_the_generated_records(self) -> None:
        structured = self.output / "service" / "structured"
        suppliers = {
            row["supplier_id"]: row for row in _rows(structured / "suppliers.csv")
        }
        exposure: dict[str, float] = {}
        for row in _rows(structured / "purchase_orders.csv"):
            if row["status_at_cutoff"] == "in_transit":
                supplier_id = row["supplier_id"]
                exposure[supplier_id] = exposure.get(supplier_id, 0) + float(
                    row["amount_usd"]
                )
        high = {
            supplier_id: amount
            for supplier_id, amount in exposure.items()
            if suppliers[supplier_id]["criticality"] == "high"
        }
        winner = max(high, key=high.get)
        atlas_premium = exposure["SUP-007"] * 0.10
        smoke = json.loads(
            (EXAMPLE_ROOT / "evaluations" / "smoke.json").read_text(encoding="utf-8")
        )
        cases = {case["id"]: case["expected"] for case in smoke["cases"]}
        self.assertEqual(
            [suppliers[winner]["supplier_name"]], cases["structured"]["facts"]
        )
        self.assertEqual([winner], cases["structured"]["citations"])
        self.assertEqual([f"{atlas_premium:,.0f}"], cases["analysis"]["facts"])

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
    def test_root_env_example_is_the_single_operator_template(self) -> None:
        template = (EXAMPLE_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertFalse((EXAMPLE_ROOT / "deploy" / ".env.example").exists())
        for name in (
            "GSF_SOURCE_DIR",
            "GSF_SOURCE_REVISION",
            "NVIDIA_INFERENCE_API_KEY",
            "KUMO_RFM_API_URL",
            "KUMO_RFM_API_KEY",
            "NEMOCLAW_SANDBOX_NAME",
            "NEMOCLAW_DASHBOARD_PORT",
            "NEMOCLAW_HERMES_API_PORT",
        ):
            self.assertEqual(1, template.count(f"\n{name}="), name)

    def test_contract_and_skill_tree_are_valid(self) -> None:
        contracts = VERIFIER.load_contracts()
        self.assertEqual({"ontology", "retriever", "kumo"}, set(contracts["routes"]))
        self.assertEqual(
            {"name": "query-claw", "token_env": "QUERY_CLAW_MCP_TOKEN"},
            contracts["registration"],
        )
        skills = {
            path.parent.name for path in (EXAMPLE_ROOT / "skills").glob("*/SKILL.md")
        }
        self.assertEqual(
            {
                "query-claw",
                "query-claw-structured",
                "query-claw-documents",
                "query-claw-predictive",
                "query-claw-reporting",
            },
            skills,
        )
        self.assertTrue(
            (EXAMPLE_ROOT / "skills/query-claw/references/evidence.md").is_file()
        )
        gsf_setup = (EXAMPLE_ROOT / "deploy/setup-gsf.sh").read_text(encoding="utf-8")
        self.assertIn('delete_all_data("query_claw")', gsf_setup)
        self.assertNotIn('delete_semantic_layer("query_claw")', gsf_setup)
        VERIFIER.validate_skill()

    def test_exact_query_only_inventory_is_accepted(self) -> None:
        tools = VERIFIER.load_contracts()["routes"]["kumo"]
        VERIFIER.check_inventory("kumo", tools, tools)

    def test_missing_or_extra_tools_fail_closed(self) -> None:
        tools = VERIFIER.load_contracts()["routes"]["kumo"]
        with self.assertRaisesRegex(ValueError, "missing required tools"):
            VERIFIER.check_inventory("kumo", tools, ["predict"])
        with self.assertRaisesRegex(ValueError, "outside its allowed contract"):
            VERIFIER.check_inventory("kumo", tools, tools + ["delete_graph"])

    def test_truncated_or_inconsistent_discovery_fails_closed(self) -> None:
        complete = {"ok": True, "count": 1, "tools": ["query"], "truncated": False}
        self.assertEqual(["query"], VERIFIER.discovery_tools("retriever", complete))
        with self.assertRaisesRegex(ValueError, "truncated"):
            VERIFIER.discovery_tools("retriever", complete | {"truncated": True})
        with self.assertRaisesRegex(ValueError, "count"):
            VERIFIER.discovery_tools("retriever", complete | {"count": 2})

    def test_native_private_bridge_accepts_only_the_v120_discovery_limit(self) -> None:
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
        self.assertIsNone(VERIFIER.managed_bridge_tools("ontology", ready))
        with self.assertRaisesRegex(ValueError, "tool discovery did not succeed"):
            VERIFIER.managed_bridge_tools(
                "ontology",
                ready
                | {
                    "toolDiscovery": ready["toolDiscovery"]
                    | {"detail": "sandbox unreachable"}
                },
            )
        with self.assertRaisesRegex(ValueError, "private bridge is not ready"):
            VERIFIER.managed_bridge_tools(
                "ontology", ready | {"adapter": {"registered": False}}
            )

    def test_adapter_arguments_match_the_skill_contract(self) -> None:
        path = EXAMPLE_ROOT / "deploy" / "services" / "mcp-adapters" / "app.py"
        adapter_source = path.read_text(encoding="utf-8")
        tree = ast.parse(adapter_source)
        arguments = {
            node.name: [argument.arg for argument in node.args.args]
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name
            in {"search_terms", "ask_question", "query", "predict", "explain"}
        }
        self.assertEqual(["query", "limit"], arguments["search_terms"])
        self.assertEqual(["question", "entity_filters"], arguments["ask_question"])
        self.assertEqual(["question", "top_k"], arguments["query"])
        self.assertEqual(25, MCP_ADAPTER.MAX_ONTOLOGY_ROWS)
        self.assertEqual(["pql", "entity_ids", "max_results"], arguments["predict"])
        self.assertEqual(["pql", "entity_id"], arguments["explain"])
        predictive_skill = (
            EXAMPLE_ROOT / "skills" / "query-claw-predictive" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("PREDICT purchase_orders", predictive_skill)
        self.assertIn('"prediction_contract"', adapter_source)
        self.assertIn("COUNT(delivery_outcomes.*", MCP_ADAPTER.KUMO_PREDICTION_PQL)
        self.assertIn("0, 30, DAYS", MCP_ADAPTER.KUMO_PREDICTION_PQL)
        self.assertIn(
            "COUNT(shipment_events.*, -30, 0, DAYS)", MCP_ADAPTER.KUMO_PREDICTION_PQL
        )

    def test_governed_entity_aliases_resolve_names_to_stable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "suppliers.csv": "supplier_id,supplier_name\nSUP-007,Atlas Circuits\n",
                "facilities.csv": "facility_id,name\nFAC-ATL,Atlanta Assembly\n",
                "products.csv": "product_id,name\nPRD-CTRL,Control module\n",
            }
            for name, contents in fixtures.items():
                (root / name).write_text(contents, encoding="utf-8")
            aliases = MCP_ADAPTER._load_entity_aliases(root)
        resolved = MCP_ADAPTER._resolve_entity_filters(["atlas circuits"], aliases)
        self.assertEqual(
            (
                {
                    "entity": "suppliers",
                    "id_field": "supplier_id",
                    "id": "SUP-007",
                    "name": "Atlas Circuits",
                },
            ),
            resolved,
        )
        normalized = MCP_ADAPTER._validate_filtered_rows(
            [{"supplier_id": "SUP-007", "amount_usd": 100}], resolved
        )
        self.assertEqual("SUP-007", normalized[0]["supplier_id"])
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "outside"):
            MCP_ADAPTER._validate_filtered_rows(
                [{"confirmed_entity_id": "SUP-007"}], resolved
            )
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "outside"):
            MCP_ADAPTER._validate_filtered_rows(
                [{"confirmed_entity_id": "SUP-008"}], resolved
            )
        self.assertEqual((), MCP_ADAPTER._resolve_entity_filters([], aliases))
        with self.assertRaisesRegex(ValueError, "governed display name"):
            MCP_ADAPTER._resolve_entity_filters(["Unknown Supplier"], aliases)

    def test_kumo_model_discovery_accepts_only_supported_ids(self) -> None:
        client = MagicMock()
        response = client.return_value.__enter__.return_value.get.return_value
        response.is_redirect = False
        response.status_code = 200
        response.json.return_value = {"data": [{"id": "kumo-relational"}]}

        with patch.object(MCP_ADAPTER.httpx, "Client", client):
            selected = MCP_ADAPTER._advertised_kumo_model(
                "https://kumo.example.test", "secret-key"
            )
        self.assertEqual("kumo-relational", selected)
        client.assert_called_once_with(follow_redirects=False, timeout=30)
        response_call = client.return_value.__enter__.return_value.get
        response_call.assert_called_once_with(
            "https://kumo.example.test/v1/models",
            headers={"X-API-Key": "secret-key"},
        )

        with patch.object(MCP_ADAPTER.httpx, "Client") as client:
            response = client.return_value.__enter__.return_value.get.return_value
            response.is_redirect = False
            response.status_code = 200
            response.json.return_value = {"data": [{"id": "other-model"}]}
            with self.assertRaisesRegex(RuntimeError, "kumo-relational"):
                MCP_ADAPTER._advertised_kumo_model("https://kumo.example.test", None)

    def test_kumo_model_discovery_does_not_follow_or_leak_secrets(self) -> None:
        secret = "do-not-disclose-this-key"
        client = MagicMock()
        client.__enter__.return_value.get.return_value = MagicMock(
            is_redirect=True, status_code=307
        )
        with patch.object(MCP_ADAPTER.httpx, "Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "must not redirect") as raised:
                MCP_ADAPTER._advertised_kumo_model("https://kumo.example.test", secret)
        self.assertNotIn(secret, str(raised.exception))

    def test_kumo_model_discovery_prefers_relational_and_preserves_base_path(
        self,
    ) -> None:
        client = MagicMock()
        response = client.return_value.__enter__.return_value.get.return_value
        response.is_redirect = False
        response.status_code = 200
        response.json.return_value = {
            "data": [{"id": "kumo-rfm"}, {"id": "kumo-relational"}]
        }
        with patch.object(MCP_ADAPTER.httpx, "Client", client):
            selected = MCP_ADAPTER._advertised_kumo_model(
                "https://kumo.example.test/tenant/api/", None
            )
        self.assertEqual("kumo-relational", selected)
        client.return_value.__enter__.return_value.get.assert_called_once_with(
            "https://kumo.example.test/tenant/api/v1/models", headers=None
        )

    def test_kumo_endpoint_accepts_https_and_loopback_http_only(self) -> None:
        accepted = {
            "https://kumo.example.test": "https://kumo.example.test",
            "https://kumo.example.test/tenant/api/": (
                "https://kumo.example.test/tenant/api"
            ),
            "http://localhost:8000/api": "http://localhost:8000/api",
            "http://127.0.0.1:8000": "http://127.0.0.1:8000",
            "http://[::1]:8000/base/": "http://[::1]:8000/base",
        }
        for endpoint, expected in accepted.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    expected, MCP_ADAPTER._validated_kumo_endpoint(endpoint)
                )

        rejected = (
            "http://kumo.example.test",
            "http://0.0.0.0:8000",
            "https://user:password@kumo.example.test",
            "https://kumo.example.test?token=secret",
            "https://kumo.example.test/#fragment",
            "ftp://kumo.example.test",
            "kumo.example.test",
        )
        for endpoint in rejected:
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(RuntimeError),
            ):
                MCP_ADAPTER._validated_kumo_endpoint(endpoint)

    def test_kumo_connects_with_the_relational_client(self) -> None:
        class FakeClient:
            def __init__(self, endpoint, api_key=None):
                self.endpoint = endpoint
                self.api_key = api_key
                self.closed = False

            def close(self):
                self.closed = True

        payload = types.SimpleNamespace(TFM_MODEL_KUMO_RFM="kumo-rfm")
        rfm = types.ModuleType("kumorfm.rfm")
        rfm.payload = payload
        rfm.KumoRFM = lambda graph, _client: ("relational-model", graph, _client)
        kumorfm = types.ModuleType("kumorfm")
        kumorfm.__path__ = []
        kumorfm.KumoClient = FakeClient
        kumorfm.rfm = rfm
        with patch.dict(sys.modules, {"kumorfm": kumorfm, "kumorfm.rfm": rfm}):
            model, client = MCP_ADAPTER._connect_kumo(
                "graph", "https://kumo.example.test", "key"
            )
        self.assertEqual(("relational-model", "graph", client), model)
        self.assertEqual("kumo-relational", payload.TFM_MODEL_KUMO_RFM)

        runtime = MCP_ADAPTER.KumoRuntime()
        runtime.client = client
        runtime.model = model
        runtime.graph = "graph"
        runtime.prediction_entity_ids = ["order-1"]
        runtime.prediction_cutoff = datetime(2026, 5, 31)
        runtime.prediction_horizon_days = 30
        runtime.close()
        self.assertTrue(client.closed)
        self.assertIsNone(runtime.model)
        self.assertIsNone(runtime.prediction_cutoff)

    def test_kumo_model_omits_prediction_population_bookkeeping(self) -> None:
        source_orders = MagicMock()
        source_frames = {
            "purchase_orders": source_orders,
            "suppliers": "supplier-frame",
        }
        model_frames = MCP_ADAPTER.KumoRuntime._model_frames(source_frames)
        source_orders.drop.assert_called_once_with(
            columns=["split", "status_at_cutoff"]
        )
        self.assertIs(source_orders, source_frames["purchase_orders"])
        self.assertIs(source_orders.drop.return_value, model_frames["purchase_orders"])
        self.assertEqual("supplier-frame", model_frames["suppliers"])

    def test_kumo_predictions_are_validated_and_ranked(self) -> None:
        rows = MCP_ADAPTER._rank_prediction_rows(
            [
                {
                    "ENTITY": "PO-E0001",
                    "ANCHOR_TIMESTAMP": "2026-05-31T00:00:00+00:00",
                    "TRUE_PROB": 0.2,
                },
                {
                    "ENTITY": "PO-E0002",
                    "ANCHOR_TIMESTAMP": "2026-05-31T00:00:00+00:00",
                    "TRUE_PROB": 0.8,
                },
            ],
            ["PO-E0001", "PO-E0002"],
            "2026-05-31",
        )
        self.assertEqual(["PO-E0002", "PO-E0001"], [row["ENTITY"] for row in rows])
        with self.assertRaisesRegex(ValueError, "wrong cutoff"):
            MCP_ADAPTER._rank_prediction_rows(
                [
                    {
                        "ENTITY": "PO-E0001",
                        "ANCHOR_TIMESTAMP": "2026-06-01T00:00:00+00:00",
                        "TRUE_PROB": 0.2,
                    }
                ],
                ["PO-E0001"],
                "2026-05-31",
            )
        for malformed_anchor in (
            "2026-05-3100",
            "2026-05-31T" + "0" * 65,
        ):
            with (
                self.subTest(anchor=malformed_anchor),
                self.assertRaisesRegex(ValueError, "wrong cutoff"),
            ):
                MCP_ADAPTER._rank_prediction_rows(
                    [
                        {
                            "ENTITY": "PO-E0001",
                            "ANCHOR_TIMESTAMP": malformed_anchor,
                            "TRUE_PROB": 0.2,
                        }
                    ],
                    ["PO-E0001"],
                    "2026-05-31",
                )


class QueryClawAdapterSecurityTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "test-query-claw-token-that-is-long-enough"

    class FakeKumoRuntime:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.prediction_entity_ids = ["PO-EVAL-001"]
            self.prediction_cutoff = datetime(2026, 5, 31)
            self.prediction_horizon_days = 30
            self.model = MagicMock()
            self.model.predict.side_effect = error
            self.graph = types.SimpleNamespace(tables={}, edges=[])

        async def ready(self) -> None:
            if self.error and self.model is None:
                raise self.error

        def close(self) -> None:
            pass

    def kumo_tools(self, runtime):
        with (
            patch.object(MCP_ADAPTER, "KumoRuntime", return_value=runtime),
            patch.dict(
                MCP_ADAPTER.os.environ,
                {"MCP_BEARER_TOKEN": self.TOKEN},
                clear=False,
            ),
        ):
            return MCP_ADAPTER.kumo_server().tools

    def test_combined_server_exposes_only_the_contract_union(self) -> None:
        contracts = VERIFIER.load_contracts()
        expected = {tool for tools in contracts["routes"].values() for tool in tools}
        with patch.dict(
            MCP_ADAPTER.os.environ,
            {
                "MCP_BEARER_TOKEN": self.TOKEN,
                "RETRIEVER_API_TOKEN": self.TOKEN,
            },
            clear=True,
        ):
            actual = set(MCP_ADAPTER.query_claw_server().tools)
        self.assertEqual(expected, actual)

    async def test_retriever_tool_forces_bounded_citation_ready_query(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "evidence": [{"text": "notice", "source": "SUP-007"}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        with (
            patch.object(MCP_ADAPTER.httpx, "AsyncClient", return_value=client),
            patch.dict(
                MCP_ADAPTER.os.environ,
                {
                    "MCP_BEARER_TOKEN": self.TOKEN,
                    "RETRIEVER_API_TOKEN": self.TOKEN,
                },
                clear=True,
            ),
        ):
            tools = MCP_ADAPTER.retriever_server().tools
        result = await tools["query"]("Atlas Circuits", top_k=5)
        self.assertEqual("SUP-007", result["evidence"][0]["source"])
        client.post.assert_awaited_once_with(
            "/v1/query",
            json={
                "query": "Atlas Circuits",
                "top_k": 5,
                "format": "evidence",
                "rerank": False,
            },
        )

    async def test_predict_and_explain_require_the_exact_advertised_pql(self) -> None:
        runtime = self.FakeKumoRuntime()
        tools = self.kumo_tools(runtime)
        for tool_name, arguments in (
            (
                "predict",
                {"entity_ids": ["PO-EVAL-001"], "max_results": 1},
            ),
            ("explain", {"entity_id": "PO-EVAL-001"}),
        ):
            with (
                self.subTest(tool=tool_name),
                self.assertRaisesRegex(
                    MCP_ADAPTER.ToolError, "exactly match.*prediction contract"
                ),
            ):
                await tools[tool_name](
                    pql=MCP_ADAPTER.KUMO_PREDICTION_PQL + " ",
                    **arguments,
                )
        runtime.model.predict.assert_not_called()

    async def test_predict_uses_the_fixed_cutoff_and_ranks_results(self) -> None:
        runtime = self.FakeKumoRuntime()
        runtime.prediction_entity_ids = ["PO-EVAL-001", "PO-EVAL-002"]
        runtime.model.predict.return_value = [
            {
                "ENTITY": "PO-EVAL-001",
                "ANCHOR_TIMESTAMP": "2026-05-31T00:00:00+00:00",
                "TRUE_PROB": 0.1,
            },
            {
                "ENTITY": "PO-EVAL-002",
                "ANCHOR_TIMESTAMP": "2026-05-31T00:00:00+00:00",
                "TRUE_PROB": 0.9,
            },
        ]
        tools = self.kumo_tools(runtime)
        result = await tools["predict"](
            pql=MCP_ADAPTER.KUMO_PREDICTION_PQL,
            entity_ids=runtime.prediction_entity_ids,
            max_results=1,
        )
        runtime.model.predict.assert_called_once_with(
            MCP_ADAPTER.KUMO_PREDICTION_PQL,
            indices=runtime.prediction_entity_ids,
            anchor_time=runtime.prediction_cutoff,
            run_mode="fast",
        )
        self.assertEqual("2026-05-31", result["cutoff"])
        self.assertEqual(30, result["horizon_days"])
        self.assertEqual("PO-EVAL-002", result["predictions"][0]["ENTITY"])
        self.assertTrue(result["truncated"])

    async def test_explain_binds_entity_and_bounds_factors(self) -> None:
        runtime = self.FakeKumoRuntime()
        cells = {
            f"factor_{index}": {
                "value": "x" * 300 if index == 13 else index,
                "score": index / 20,
            }
            for index in range(1, 14)
        }
        runtime.model.predict.return_value = types.SimpleNamespace(
            prediction=[
                {
                    "ENTITY": "PO-EVAL-001",
                    "ANCHOR_TIMESTAMP": "2026-05-31T00:00:00+00:00",
                    "TRUE_PROB": 0.25,
                    "UNEXPECTED": "not returned",
                }
            ],
            details={
                "format": "kumo_rfm_v2_1",
                "details": {
                    "task_type": "binary_classification",
                    "cohorts": ["not returned"],
                    "subgraphs": [
                        {
                            "tables": {
                                "purchase_orders": {
                                    "0": {
                                        "cells": cells,
                                        "links": {"not": "returned"},
                                    }
                                }
                            },
                            "context_examples": ["not returned"],
                        }
                    ],
                },
            },
        )
        tools = self.kumo_tools(runtime)
        result = await tools["explain"](
            pql=MCP_ADAPTER.KUMO_PREDICTION_PQL,
            entity_id="PO-EVAL-001",
        )
        runtime.model.predict.assert_called_once_with(
            MCP_ADAPTER.KUMO_PREDICTION_PQL,
            indices=["PO-EVAL-001"],
            anchor_time=runtime.prediction_cutoff,
            run_mode="fast",
            explain={"skip_summary": True},
        )
        self.assertEqual("PO-EVAL-001", result["entity_id"])
        self.assertEqual(
            {
                "entity_id": "PO-EVAL-001",
                "anchor_timestamp": "2026-05-31T00:00:00+00:00",
                "late_probability": 0.25,
            },
            result["prediction"],
        )
        factors = result["explanation"]["factors"]
        self.assertEqual(12, len(factors))
        self.assertEqual("factor_13", factors[0]["column"])
        self.assertEqual(256, len(factors[0]["value"]))
        self.assertTrue(result["explanation"]["truncated"])
        serialized = json.dumps(result)
        self.assertNotIn("context_examples", serialized)
        self.assertNotIn("links", serialized)
        self.assertNotIn("UNEXPECTED", serialized)
        runtime.model.predict.return_value.prediction[0]["ENTITY"] = "PO-EVAL-999"
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "Kumo explanation failed"):
            await tools["explain"](
                pql=MCP_ADAPTER.KUMO_PREDICTION_PQL,
                entity_id="PO-EVAL-001",
            )

    async def test_kumo_upstream_failures_do_not_expose_details(self) -> None:
        secret = "upstream-secret-and-internal-host"
        runtime = self.FakeKumoRuntime(RuntimeError(secret))
        tools = self.kumo_tools(runtime)
        with self.assertRaises(MCP_ADAPTER.ToolError) as prediction:
            await tools["predict"](
                pql=MCP_ADAPTER.KUMO_PREDICTION_PQL,
                entity_ids=["PO-EVAL-001"],
            )
        self.assertEqual("Kumo prediction failed", str(prediction.exception))
        self.assertNotIn(secret, str(prediction.exception))

        with self.assertRaises(MCP_ADAPTER.ToolError) as explanation:
            await tools["explain"](
                pql=MCP_ADAPTER.KUMO_PREDICTION_PQL,
                entity_id="PO-EVAL-001",
            )
        self.assertEqual("Kumo explanation failed", str(explanation.exception))
        self.assertNotIn(secret, str(explanation.exception))

    async def test_kumo_readiness_failure_is_redacted(self) -> None:
        secret = "private-readiness-diagnostic"
        runtime = self.FakeKumoRuntime(RuntimeError(secret))
        runtime.model = None
        tools = self.kumo_tools(runtime)
        with self.assertRaises(MCP_ADAPTER.ToolError) as raised:
            await tools["inspect_graph_metadata"]()
        self.assertEqual("Kumo service is unavailable", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    async def test_ontology_stream_error_is_redacted(self) -> None:
        secret = "database-secret-and-internal-host"

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                yield "data: " + json.dumps({"type": "error", "message": secret})

        client = MagicMock()
        client.stream.return_value = Response()
        with (
            patch.object(MCP_ADAPTER.httpx, "AsyncClient", return_value=client),
            patch.dict(
                MCP_ADAPTER.os.environ,
                {"MCP_BEARER_TOKEN": self.TOKEN},
                clear=False,
            ),
        ):
            tools = MCP_ADAPTER.ontology_server().tools
        with self.assertRaises(MCP_ADAPTER.ToolError) as raised:
            await tools["ask_question"]("Which supplier is at risk?")
        self.assertEqual("Ontology agent failed", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        sent_question = client.stream.call_args.kwargs["json"]["question"]
        self.assertTrue(sent_question.startswith("Which supplier is at risk?\n\n"))
        self.assertIn("never compare an identifier column", sent_question)
        self.assertIn("stable entity identifier columns", sent_question)

    async def test_ontology_rejects_rows_outside_an_explicit_entity_filter(
        self,
    ) -> None:
        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                result = {
                    "type": "result",
                    "answer": {
                        "sql_code": "SELECT amount_usd FROM purchase_orders",
                        "sql_response_from_db": [
                            {"supplier_id": "SUP-008", "amount_usd": 100}
                        ],
                    },
                }
                yield "data: " + json.dumps(result)

        client = MagicMock()
        client.stream.return_value = Response()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, contents in {
                "suppliers.csv": "supplier_id,supplier_name\nSUP-007,Atlas Circuits\n",
                "facilities.csv": "facility_id,name\nFAC-ATL,Atlanta Assembly\n",
                "products.csv": "product_id,name\nPRD-CTRL,Control module\n",
            }.items():
                (root / name).write_text(contents, encoding="utf-8")
            with (
                patch.object(MCP_ADAPTER.httpx, "AsyncClient", return_value=client),
                patch.dict(
                    MCP_ADAPTER.os.environ,
                    {
                        "MCP_BEARER_TOKEN": self.TOKEN,
                        "QUERY_CLAW_DATA_DIR": str(root),
                    },
                    clear=False,
                ),
            ):
                tools = MCP_ADAPTER.ontology_server().tools
            with self.assertRaisesRegex(
                MCP_ADAPTER.ToolError, "outside the requested entity filters"
            ):
                await tools["ask_question"](
                    "Show Atlas Circuits orders",
                    entity_filters=["Atlas Circuits"],
                )

        sent_question = client.stream.call_args.kwargs["json"]["question"]
        self.assertIn('"id":"SUP-007"', sent_question)
        self.assertIn('"id_field":"supplier_id"', sent_question)

    async def test_ontology_requires_verifiable_rows_for_entity_filters(self) -> None:
        missing = object()

        async def invoke(rows, entity_filters=("Atlas Circuits",)):
            answer = {"sql_code": "SELECT supplier_id FROM suppliers"}
            if rows is not missing:
                answer["sql_response_from_db"] = rows

            class Response:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                def raise_for_status(self):
                    pass

                async def aiter_lines(self):
                    yield "data: " + json.dumps({"type": "result", "answer": answer})

            client = MagicMock()
            client.stream.return_value = Response()
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name, contents in {
                    "suppliers.csv": (
                        "supplier_id,supplier_name\nSUP-007,Atlas Circuits\n"
                    ),
                    "facilities.csv": "facility_id,name\nFAC-ATL,Atlanta Assembly\n",
                    "products.csv": "product_id,name\nPRD-CTRL,Control module\n",
                }.items():
                    (root / name).write_text(contents, encoding="utf-8")
                with (
                    patch.object(MCP_ADAPTER.httpx, "AsyncClient", return_value=client),
                    patch.dict(
                        MCP_ADAPTER.os.environ,
                        {
                            "MCP_BEARER_TOKEN": self.TOKEN,
                            "QUERY_CLAW_DATA_DIR": str(root),
                        },
                        clear=False,
                    ),
                ):
                    tools = MCP_ADAPTER.ontology_server().tools
                return await tools["ask_question"](
                    "Show Atlas Circuits orders",
                    entity_filters=list(entity_filters),
                )

        for rows in (missing, ["not-json"], {"unexpected": "object"}):
            with (
                self.subTest(rows=rows),
                self.assertRaisesRegex(MCP_ADAPTER.ToolError, "invalid rows"),
            ):
                await invoke(rows)
            with (
                self.subTest(rows=rows, unfiltered=True),
                self.assertRaisesRegex(MCP_ADAPTER.ToolError, "invalid rows"),
            ):
                await invoke(rows, entity_filters=())

        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "no verifiable rows"):
            await invoke([])

        result = await invoke([{"supplier_id": "SUP-007", "amount_usd": 100}])
        self.assertEqual("SUP-007", result["rows"][0]["supplier_id"])

    async def test_ontology_caps_rows_and_omits_unbounded_upstream_prose(self) -> None:
        rows = [{"supplier_id": f"SUP-{index:03d}"} for index in range(26)]

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                answer = {
                    "response": "UNBOUNDED-SENTINEL",
                    "sql_code": "SELECT supplier_id FROM suppliers",
                    "sql_response_from_db": rows,
                }
                yield "data: " + json.dumps({"type": "result", "answer": answer})

        client = MagicMock()
        client.stream.return_value = Response()
        with (
            patch.object(MCP_ADAPTER.httpx, "AsyncClient", return_value=client),
            patch.dict(
                MCP_ADAPTER.os.environ,
                {"MCP_BEARER_TOKEN": self.TOKEN},
                clear=False,
            ),
        ):
            tools = MCP_ADAPTER.ontology_server().tools
        result = await tools["ask_question"]("List suppliers")
        self.assertEqual(25, len(result["rows"]))
        self.assertEqual(26, result["row_count"])
        self.assertTrue(result["truncated"])
        self.assertNotIn("answer", result)


class QueryClawLiveEvaluatorTests(unittest.TestCase):
    @staticmethod
    def events(tools: list[str], *, output: str = "answer") -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for name in tools:
            events.append({"event": "tool.started", "tool": name})
            if name == "execute_code":
                events.extend(
                    [
                        {
                            "event": "approval.request",
                            "pattern_key": "execute_code",
                            "pattern_keys": ["execute_code"],
                            "command": (
                                "execute_code <<'PY'\n"
                                "exposure = 476680\nprint(exposure * 0.1)\nPY"
                            ),
                            "choices": ["once", "session", "always", "deny"],
                        },
                        {
                            "event": "approval.responded",
                            "choice": "once",
                            "resolved": 1,
                        },
                    ]
                )
            events.append({"event": "tool.completed", "tool": name, "error": False})
        events.append({"event": "run.completed", "output": output})
        return events

    @staticmethod
    def expected_output(case) -> str:
        terms = [*case.required_answer_terms, *case.required_citation_terms]
        if case.expect_abstention:
            terms.append("cannot answer")
        return " ".join(terms) or "answer"

    def assert_run_error(
        self,
        case,
        tools: list[str],
        message: str,
        *,
        output: str | None = None,
        events: list[dict[str, object]] | None = None,
        status: dict[str, object] | None = None,
    ) -> None:
        output = self.expected_output(case) if output is None else output
        with self.assertRaisesRegex(LIVE_EVALUATOR.EvaluationError, message):
            LIVE_EVALUATOR.validate_run(
                case,
                self.events(tools, output=output) if events is None else events,
                {"status": "completed", "output": output} if status is None else status,
            )

    @staticmethod
    def approval(case):
        return next(
            event
            for event in QueryClawLiveEvaluatorTests.events(["execute_code"])
            if event["event"] == "approval.request"
        )

    def test_tool_routes_come_from_the_canonical_contract(self) -> None:
        document = json.loads(
            (EXAMPLE_ROOT / "config" / "tool-contracts.json").read_text(
                encoding="utf-8"
            )
        )
        prefix = document["registration"]["name"].replace("-", "_")
        expected = {
            route: {f"mcp__{prefix}__{name}" for name in names}
            for route, names in document["routes"].items()
        }
        self.assertEqual(
            expected,
            {route: set(tools) for route, tools in LIVE_EVALUATOR.ROUTE_TOOLS.items()},
        )

    def test_public_suites_and_every_smoke_route_validate(self) -> None:
        evaluations = EXAMPLE_ROOT / "evaluations"
        smoke_name, smoke = LIVE_EVALUATOR.load_suite(evaluations / "smoke.json")
        scenarios_name, scenarios = LIVE_EVALUATOR.load_suite(
            evaluations / "scenarios.json"
        )
        self.assertEqual(("query-claw-smoke", 6), (smoke_name, len(smoke)))
        self.assertEqual(("query-claw-scenarios", 6), (scenarios_name, len(scenarios)))
        self.assertEqual(len(smoke), len({name for name, _ in smoke}))
        self.assertTrue(dict(smoke)["abstention"].expect_abstention)
        self.assertEqual(
            ["retriever", "ontology"],
            [
                next(iter(case.routes))
                for _, case in scenarios
                if case.session == "source-switch"
            ],
        )

        for name, case in smoke:
            tools = [*case.order, *sorted(case.required - set(case.order))]
            output = self.expected_output(case)
            with self.subTest(case=name):
                self.assertEqual(
                    tools,
                    LIVE_EVALUATOR.validate_run(
                        case,
                        self.events(tools, output=output),
                        {"status": "completed", "output": output},
                    ),
                )

        case = SMOKE_CASES["unstructured"]
        output = self.expected_output(case)
        tools = ["skills_list", "skill_view", "skill_view", *case.order]
        self.assertEqual(
            tools,
            LIVE_EVALUATOR.validate_run(
                case,
                self.events(tools, output=output),
                {"status": "completed", "output": output},
            ),
        )
        for builtin in ("skill_manage", "memory"):
            with self.subTest(builtin=builtin):
                self.assert_run_error(
                    case,
                    [builtin, *case.order],
                    "out-of-contract builtin",
                )

    def test_jsonl_suite_and_case_contract_fail_closed(self) -> None:
        raw = {
            "id": "external-case",
            "prompt": "Find the indexed notice.",
            "expected": {
                "routes": ["retriever"],
                "tools": ["retriever.query"],
                "tool_order": ["retriever.query"],
                "facts": [],
                "citations": [],
                "abstain": False,
            },
        }
        with tempfile.TemporaryDirectory(prefix="query-claw-suite-") as directory:
            path = Path(directory) / "private.jsonl"
            path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            name, cases = LIVE_EVALUATOR.load_suite(path)
        self.assertEqual(
            ("private", ["external-case"]), (name, [item[0] for item in cases])
        )

        invalid_cases = (
            (
                raw | {"expected": raw["expected"] | {"tools": ["retriever.delete"]}},
                "outside",
            ),
            (
                raw
                | {"expected": raw["expected"] | {"routes": ["retriever", "ontology"]}},
                "cover exactly",
            ),
            (raw | {"unexpected": True}, "unknown fields"),
        )
        for invalid, message in invalid_cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(LIVE_EVALUATOR.EvaluationError, message),
            ):
                LIVE_EVALUATOR.parse_suite_case(invalid, "test")

    def test_calculation_flag_and_execute_code_are_coupled(self) -> None:
        raw = next(
            case
            for case in json.loads(
                (EXAMPLE_ROOT / "evaluations" / "smoke.json").read_text(
                    encoding="utf-8"
                )
            )["cases"]
            if case["id"] == "analysis"
        )
        _, parsed = LIVE_EVALUATOR.parse_suite_case(raw, "test")
        self.assertTrue(parsed.allow_calculation)
        self.assertEqual(frozenset({"execute_code"}), parsed.allowed_builtin)
        self.assertIn("execute_code", parsed.required)

        variants = (
            {key: value for key, value in raw.items() if key != "allow_calculation"},
            raw
            | {
                "expected": raw["expected"]
                | {
                    "tools": ["ontology.ask_question"],
                    "tool_order": ["ontology.ask_question"],
                }
            },
        )
        for invalid in variants:
            with self.assertRaisesRegex(LIVE_EVALUATOR.EvaluationError, "execute_code"):
                LIVE_EVALUATOR.parse_suite_case(invalid, "test")

    def test_named_sessions_are_consecutive_and_selected_in_order(self) -> None:
        path = EXAMPLE_ROOT / "evaluations" / "scenarios.json"
        _, scenarios = LIVE_EVALUATOR.load_suite(path)
        planned = LIVE_EVALUATOR.plan_sessions(scenarios)
        documents = next(item for item in planned if item[0] == "documents-only")
        structured = next(item for item in planned if item[0] == "switch-to-structured")
        self.assertEqual(documents[2], structured[2])
        self.assertEqual((False, True), (documents[3], structured[3]))
        self.assertTrue(all(item[2] is None for item in planned if not item[1].session))

        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["cases"].insert(2, raw["cases"][0] | {"id": "interruption"})
        with tempfile.TemporaryDirectory(prefix="query-claw-suite-") as directory:
            invalid_path = Path(directory) / "nonconsecutive.json"
            invalid_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                LIVE_EVALUATOR.EvaluationError, "must be consecutive"
            ):
                LIVE_EVALUATOR.load_suite(invalid_path)

        with self.assertRaisesRegex(
            LIVE_EVALUATOR.EvaluationError, "requires earlier session"
        ):
            LIVE_EVALUATOR.select_cases(scenarios, ["switch-to-structured"])
        selected = LIVE_EVALUATOR.select_cases(
            scenarios, ["documents-only", "switch-to-structured"]
        )
        self.assertEqual(
            ["documents-only", "switch-to-structured"],
            [name for name, _ in selected],
        )

    def test_tool_route_and_output_contracts_fail_closed(self) -> None:
        unstructured = SMOKE_CASES["unstructured"]
        predictive = SMOKE_CASES["predictive"]
        structured = SMOKE_CASES["structured"]
        retried = [*unstructured.order, *unstructured.order]
        self.assertEqual(
            retried,
            LIVE_EVALUATOR.validate_run(
                unstructured,
                self.events(retried, output=self.expected_output(unstructured)),
                {
                    "status": "completed",
                    "output": self.expected_output(unstructured),
                },
            ),
        )
        failures = [
            (
                "cross-route",
                unstructured,
                [*unstructured.order, LIVE_EVALUATOR.tool("kumo", "predict")],
                "out-of-contract MCP",
            ),
            *[
                (
                    fallback,
                    unstructured,
                    [*unstructured.order, fallback],
                    message,
                )
                for fallback, message in (
                    ("mcp__other__read", "out-of-contract MCP"),
                    ("browser_navigate", "web fallback"),
                    ("web_extract", "web fallback"),
                    ("execute_code", "out-of-contract builtin"),
                    ("filesystem_write", "out-of-contract builtin"),
                )
            ],
            (
                "route-order",
                predictive,
                list(reversed(predictive.order)),
                "out of order",
            ),
            (
                "repeated-query",
                unstructured,
                [*unstructured.order, *unstructured.order, *unstructured.order],
                "call bound",
            ),
            (
                "missing-tool",
                predictive,
                list(predictive.order[:-1]),
                "required tools",
            ),
            (
                "extra-tool",
                structured,
                [
                    *structured.order,
                    LIVE_EVALUATOR.tool("ontology", "check_readiness"),
                ],
                "unexpected successful tools",
            ),
        ]
        for name, case, tools, message in failures:
            with self.subTest(case=name):
                self.assert_run_error(case, tools, message)

        self.assert_run_error(
            predictive, list(predictive.order), "nonempty answer", output=""
        )
        self.assert_run_error(
            SMOKE_CASES["abstention"],
            [],
            "did not match",
            output="The answer is 42.",
            events=self.events([], output="I cannot answer."),
        )
        self.assert_run_error(
            structured,
            list(structured.order),
            "deterministic evidence",
            output="answer",
        )

        citation_case = LIVE_EVALUATOR.Case(
            prompt="question",
            required_tools=frozenset(
                {LIVE_EVALUATOR.tool("ontology", "check_readiness")}
            ),
            required_answer_terms=("expected fact",),
            required_citation_terms=("record-42",),
        )
        self.assert_run_error(
            citation_case,
            list(citation_case.required),
            "citation",
            output="Expected fact",
        )

        tool_error = self.events(
            list(unstructured.order), output=self.expected_output(unstructured)
        )
        tool_error[1]["error"] = True
        self.assert_run_error(
            unstructured,
            list(unstructured.order),
            "required tools",
            events=tool_error,
        )

        retried_after_transport_error = self.events(
            [*unstructured.order, *unstructured.order],
            output=self.expected_output(unstructured),
        )
        retried_after_transport_error[1]["error"] = True
        self.assertEqual(
            [*unstructured.order, *unstructured.order],
            LIVE_EVALUATOR.validate_run(
                unstructured,
                retried_after_transport_error,
                {
                    "status": "completed",
                    "output": self.expected_output(unstructured),
                },
            ),
        )

    def test_abstention_and_markdown_contracts(self) -> None:
        abstention = SMOKE_CASES["abstention"]
        accepted = (
            "I cannot answer from the available evidence.",
            "I can’t help disclose private personal information.",
            "Salesforce is not an available configured source.",
            "I can’t access Salesforce opportunity data here.",
            "Salesforce is not one of the configured views.",
            "Salesforce is not configured.",
            "Salesforce is unavailable.",
            "There is insufficient information.",
        )
        for answer in accepted:
            with self.subTest(answer=answer):
                LIVE_EVALUATOR.validate_run(
                    abstention,
                    self.events([], output=answer),
                    {"status": "completed", "output": answer},
                )
        for answer in (
            "Privacy concerns aside, the home address is 123 Main Street.",
            "The address is not missing: it is 123 Main Street.",
            "I found the private personal address: 123 Main Street.",
        ):
            with self.subTest(answer=answer):
                self.assert_run_error(abstention, [], "clearly abstain", output=answer)

        _, scenarios = LIVE_EVALUATOR.load_suite(
            EXAMPLE_ROOT / "evaluations" / "scenarios.json"
        )
        report = dict(scenarios)["markdown-report"]
        answer = (
            "# Findings\nsubstrate shortage\n\n"
            "## Evidence\nSUP-007 alternate sourcing\n\n"
            "## Uncertainty\nCurrent only as of the cited notice."
        )
        LIVE_EVALUATOR.validate_run(
            report,
            self.events(list(report.order), output=answer),
            {"status": "completed", "output": answer},
        )
        self.assert_run_error(
            report,
            list(report.order),
            "Markdown",
            output="substrate shortage, alternate sourcing (SUP-007)",
        )

    def test_calculation_approval_is_bounded_arithmetic_only(self) -> None:
        case = SMOKE_CASES["analysis"]
        approval = self.approval(case)
        LIVE_EVALUATOR.validate_calculation_approval(case, approval)

        invalid = (
            ("import os\nos.system('id')", "arithmetic-only"),
            ("print(10 ** 10 ** 10)", "exponentiation"),
            ("print('x' * 1000000000)", "repetition"),
            ("print(1000000001)", "oversized number"),
            (f"print({'9' * 1500})", "oversized number"),
            (
                "chunk = 'x'\ncount = 1000000000\nprint(chunk * count)",
                "repetition",
            ),
            ("value = 1", "exactly one numeric result"),
            ("print(f'{1:1000000000}')", "arithmetic-only"),
            ("value = 1000000000\nprint(value * value)", "arithmetic bound"),
            ("print(1)\nprint(2)", "exactly one numeric result"),
            (
                "\n".join(f"value_{index} = {index}" for index in range(65))
                + "\nprint(value_0)",
                "arithmetic bound",
            ),
        )
        for script, message in invalid:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(LIVE_EVALUATOR.EvaluationError, message),
            ):
                LIVE_EVALUATOR.validate_calculation_approval(
                    case,
                    approval | {"command": f"execute_code <<'PY'\n{script}\nPY"},
                )
        with self.assertRaisesRegex(LIVE_EVALUATOR.EvaluationError, "unsafe"):
            LIVE_EVALUATOR.validate_calculation_approval(
                case, approval | {"smart_denied": True}
            )

        assignments = "\n".join(
            f"exposure_{index} = {index * 1000}.0" for index in range(1, 25)
        )
        LIVE_EVALUATOR.validate_calculation_approval(
            case,
            approval
            | {
                "command": (
                    f"execute_code <<'PY'\n{assignments}\n"
                    "total = exposure_1 + exposure_24\n"
                    "rate = 0.1\n"
                    "premium = round(total * rate, 2)\nprint(premium)\nPY"
                )
            },
        )
        self.assert_run_error(
            case,
            [*case.order, "execute_code"],
            "call bound",
        )

    def test_loopback_port_contract(self) -> None:
        for port in ("1", "8642", "49152", "65535"):
            with self.subTest(port=port):
                self.assertEqual(
                    f"http://127.0.0.1:{int(port)}",
                    LIVE_EVALUATOR.loopback_api_url(port),
                )
        for port in ("", "0", "65536", "-1", "443/path"):
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(
                    LIVE_EVALUATOR.EvaluationError, "between 1 and 65535"
                ),
            ):
                LIVE_EVALUATOR.loopback_api_url(port)

    def test_second_calculation_approval_stops_the_run(self) -> None:
        case = SMOKE_CASES["analysis"]
        approval = self.approval(case)
        with (
            patch.object(
                LIVE_EVALUATOR,
                "request_json",
                side_effect=[
                    (202, {"run_id": "run_test"}),
                    (200, {"choice": "once", "resolved": 1}),
                ],
            ) as request,
            patch.object(
                LIVE_EVALUATOR,
                "stream_events",
                return_value=iter([approval, approval]),
            ),
            patch.object(LIVE_EVALUATOR, "stop_run"),
            patch.object(LIVE_EVALUATOR, "delete_session"),
        ):
            with self.assertRaisesRegex(
                LIVE_EVALUATOR.EvaluationError, "more than one"
            ):
                LIVE_EVALUATOR.run_case(
                    "analysis", case, "http://127.0.0.1:8642", "test-key", 10
                )
        self.assertEqual(2, request.call_count)

    def test_unexpected_approval_stops_and_deletes_the_session(self) -> None:
        case = SMOKE_CASES["unstructured"]
        with (
            patch.object(
                LIVE_EVALUATOR,
                "request_json",
                return_value=(202, {"run_id": "run_test"}),
            ),
            patch.object(
                LIVE_EVALUATOR,
                "stream_events",
                return_value=iter([{"event": "approval.request"}]),
            ),
            patch.object(LIVE_EVALUATOR, "stop_run") as stop,
            patch.object(LIVE_EVALUATOR, "delete_session") as delete,
        ):
            with self.assertRaisesRegex(
                LIVE_EVALUATOR.EvaluationError, "unexpected approval"
            ):
                LIVE_EVALUATOR.run_case(
                    "unstructured",
                    case,
                    "http://127.0.0.1:8642",
                    "test-key",
                    10,
                    session_id="query-claw-eval-shared",
                    delete_after=False,
                )
        stop.assert_called_once_with("http://127.0.0.1:8642", "test-key", "run_test")
        delete.assert_called_once()

    def test_shared_session_and_bounded_approval_submission(self) -> None:
        for name in ("unstructured", "analysis"):
            case = SMOKE_CASES[name]
            answer = self.expected_output(case)
            responses = [(202, {"run_id": "run_test"})]
            if case.allow_calculation:
                responses.append((200, {"choice": "once", "resolved": 1}))
            responses.append((200, {"status": "completed", "output": answer}))
            delete_after = case.allow_calculation
            if delete_after:
                responses.append((200, {"deleted": True}))
            with (
                self.subTest(case=name),
                patch.object(
                    LIVE_EVALUATOR, "request_json", side_effect=responses
                ) as request,
                patch.object(
                    LIVE_EVALUATOR,
                    "stream_events",
                    return_value=iter(self.events(list(case.order), output=answer)),
                ),
                patch.object(LIVE_EVALUATOR, "delete_session") as delete,
            ):
                result = LIVE_EVALUATOR.run_case(
                    name,
                    case,
                    "http://127.0.0.1:8642",
                    "test-key",
                    10,
                    session_id="query-claw-eval-shared",
                    delete_after=delete_after,
                )
            submitted = request.call_args_list[0].kwargs["payload"]
            self.assertEqual("query-claw-eval-shared", submitted["session_id"])
            self.assertIn('{"skill_name":"query-claw"}', submitted["instructions"])
            if case.allow_calculation:
                self.assertIn("execute_code only", submitted["instructions"])
                self.assertIn("numeric assignments", submitted["instructions"])
                approvals = [
                    call
                    for call in request.call_args_list
                    if "/approval" in call.args[2]
                ]
                self.assertEqual({"choice": "once"}, approvals[0].kwargs["payload"])
                self.assertIn("execute_code", result.tools)
            else:
                delete.assert_not_called()

    def test_gold_checks_are_not_sent_to_hermes(self) -> None:
        query = LIVE_EVALUATOR.tool("retriever", "query")
        case = LIVE_EVALUATOR.Case(
            prompt="Find the supplier notice.",
            order=(query,),
            required_answer_terms=("DO_NOT_SEND_GOLD",),
            required_citation_terms=("record-42",),
        )
        answer = "DO_NOT_SEND_GOLD — record-42"
        with (
            patch.object(
                LIVE_EVALUATOR,
                "request_json",
                side_effect=[
                    (202, {"run_id": "run_test"}),
                    (200, {"status": "completed", "output": answer}),
                    (200, {"deleted": True}),
                ],
            ) as request,
            patch.object(
                LIVE_EVALUATOR,
                "stream_events",
                return_value=iter(self.events([query], output=answer)),
            ),
        ):
            result = LIVE_EVALUATOR.run_case(
                "private", case, "http://127.0.0.1:8642", "test-key", 10
            )
        submitted = json.dumps(request.call_args_list[0].kwargs["payload"])
        self.assertNotIn("DO_NOT_SEND_GOLD", submitted)
        self.assertNotIn("record-42", submitted)
        self.assertEqual((query,), result.tools)

    def test_main_continues_and_writes_private_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="query-claw-results-") as directory:
            output = Path(directory) / "run.jsonl"
            with (
                patch("builtins.print"),
                patch.dict(
                    LIVE_EVALUATOR.os.environ,
                    {
                        "API_SERVER_KEY": "test-key",
                        "NEMOCLAW_MODEL": "declared/model",
                    },
                ),
                patch.object(
                    LIVE_EVALUATOR.sys,
                    "argv",
                    [
                        "evaluate_live.py",
                        "--case",
                        "structured",
                        "--case",
                        "unstructured",
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(
                    LIVE_EVALUATOR,
                    "run_case",
                    side_effect=[
                        LIVE_EVALUATOR.EvaluationError(
                            "first failed",
                            LIVE_EVALUATOR.RunResult(("tool_search",)),
                        ),
                        LIVE_EVALUATOR.RunResult(("tool_search",)),
                    ],
                ) as run_case,
            ):
                self.assertEqual(1, LIVE_EVALUATOR.main())
            records = [json.loads(line) for line in output.read_text().splitlines()]
            output_mode = output.stat().st_mode & 0o777

        self.assertEqual(2, run_case.call_count)
        self.assertEqual([False, True], [record["passed"] for record in records])
        self.assertEqual(records[0]["receipt_id"], records[1]["receipt_id"])
        self.assertEqual(records[0]["suite_sha256"], records[1]["suite_sha256"])
        self.assertTrue(all(record["run_started_at"] for record in records))
        self.assertTrue(
            all(
                record["declared_stack_versions"]
                == {
                    "nemoclaw": "0.0.120",
                    "hermes": "0.20.6",
                    "openshell": "0.0.106",
                }
                for record in records
            )
        )
        self.assertTrue(
            all(record["declared_model"] == "declared/model" for record in records)
        )
        self.assertTrue(all("usage" not in record for record in records))
        self.assertEqual(0o600, output_mode)

    def test_judge_contract_request_and_private_receipt(self) -> None:
        judge = LIVE_EVALUATOR.evaluation_judge
        good = {
            "scores": {dimension: 2 for dimension in judge.DIMENSIONS},
            "material_hallucination": False,
        }
        result = judge.parse_result(good)
        self.assertTrue(result.passed)
        self.assertEqual({"scores", "material_hallucination"}, set(result.receipt()))
        self.assertFalse(
            judge.parse_result(good | {"material_hallucination": True}).passed
        )
        for invalid in (
            good | {"scores": good["scores"] | {"groundedness": 3}},
            good | {"unexpected": "free-form reason"},
        ):
            with self.assertRaises(judge.JudgeError):
                judge.parse_result(invalid)
        with self.assertRaisesRegex(judge.JudgeError, "HTTPS"):
            judge._chat_completions_url("http://judge.example.test/v1")

        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": json.dumps(good)}}]}
        ).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(judge, "build_opener", return_value=opener):
            judged = judge.score_answer(
                base_url="https://judge.example.test/v1",
                api_key="judge-key",
                model="judge-model",
                question="PRIVATE_QUESTION",
                answer="PRIVATE_ANSWER",
                expected_facts=("PRIVATE_GOLD",),
                expected_citations=("SUP-007",),
                expected_routes=("retriever",),
                expected_format="markdown_report",
                expect_abstention=False,
                timeout=10,
            )
        submitted = json.loads(opener.open.call_args.args[0].data)
        judge_input = json.loads(submitted["messages"][1]["content"])
        self.assertTrue(judged.passed)
        self.assertEqual("PRIVATE_QUESTION", judge_input["question"])
        self.assertEqual(["PRIVATE_GOLD"], judge_input["minimum_checks"]["facts"])
        schema = submitted["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            list(judge.DIMENSIONS),
            schema["properties"]["scores"]["required"],
        )
        self.assertNotIn("PRIVATE_QUESTION", json.dumps(judged.receipt()))

    def test_judged_receipt_excludes_prompt_answer_and_gold(self) -> None:
        judge = LIVE_EVALUATOR.evaluation_judge
        answer = "PRIVATE_ANSWER Willow Memory SUP-009"
        judged = judge.JudgeResult(
            {dimension: 2 for dimension in judge.DIMENSIONS}, False
        )
        with tempfile.TemporaryDirectory(prefix="query-claw-results-") as directory:
            output = Path(directory) / "run.jsonl"
            with (
                patch("builtins.print"),
                patch.dict(
                    LIVE_EVALUATOR.os.environ,
                    {
                        "API_SERVER_KEY": "hermes-key",
                        "QUERY_CLAW_JUDGE_API_KEY": "judge-key",
                    },
                ),
                patch.object(
                    LIVE_EVALUATOR.sys,
                    "argv",
                    [
                        "evaluate_live.py",
                        "--case",
                        "structured",
                        "--judge",
                        "--judge-base-url",
                        "https://judge.example.test/v1",
                        "--judge-model",
                        "judge-model",
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(
                    LIVE_EVALUATOR,
                    "run_case",
                    return_value=LIVE_EVALUATOR.RunResult(
                        (LIVE_EVALUATOR.tool("ontology", "ask_question"),), answer
                    ),
                ),
                patch.object(judge, "score_answer", return_value=judged) as score,
            ):
                self.assertEqual(0, LIVE_EVALUATOR.main())
            serialized = output.read_text(encoding="utf-8")
            record = json.loads(serialized)
        for secret in ("PRIVATE_ANSWER", "Willow Memory", "SUP-009"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(judged.receipt(), record["judge"])
        self.assertEqual("Willow Memory", score.call_args.kwargs["expected_facts"][0])

    def test_existing_private_output_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="query-claw-results-") as directory:
            output = Path(directory) / "run.jsonl"
            output.write_text("existing\n", encoding="utf-8")
            with self.assertRaisesRegex(
                LIVE_EVALUATOR.EvaluationError, "could not create private"
            ):
                LIVE_EVALUATOR.prepare_private_output(output)
            self.assertEqual("existing\n", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
