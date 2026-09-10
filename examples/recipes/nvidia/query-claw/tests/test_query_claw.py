# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import csv
import importlib.util
import json
import stat
import sys
import tempfile
import types
import unittest
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
    requests = types.ModuleType("starlette.requests")
    requests.Request = type("Request", (), {})
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
        "starlette.requests": requests,
        "starlette.responses": responses,
    }
    path = EXAMPLE_ROOT / "deploy" / "services" / "mcp-adapters" / "app.py"
    spec = importlib.util.spec_from_file_location("query_claw_mcp_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


def _load_prediction_qualifier():
    path = EXAMPLE_ROOT / "deploy" / "qualify_prediction.py"
    spec = importlib.util.spec_from_file_location("query_claw_qualify_prediction", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREDICTION_QUALIFIER = _load_prediction_qualifier()


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
            VERIFIER.check_inventory("kumo", tools, [])
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


class QueryClawPredictionQualificationTests(unittest.TestCase):
    def test_receipt_requires_kumo_qualified_prediction_and_is_private(self) -> None:
        fallback = {"sql_code": "SELECT id FROM records", "rows": [{"id": 1}]}
        with self.assertRaisesRegex(RuntimeError, "no qualified Kumo prediction"):
            PREDICTION_QUALIFIER._validate_prediction(fallback, "alpha_predictions")

        prediction = {
            "sql_code": "PREDICT records.risk FOR EACH records.id",
            "rows": [{"id": 1, "score": 0.75}],
        }
        with self.assertRaisesRegex(RuntimeError, "no qualified Kumo prediction"):
            PREDICTION_QUALIFIER._validate_prediction(prediction, "alpha_predictions")
        with self.assertRaisesRegex(RuntimeError, "no qualified Kumo prediction"):
            PREDICTION_QUALIFIER._validate_prediction(
                prediction | {"graph_receipt": {"database_name": "other"}},
                "alpha_predictions",
            )

        answer = prediction | {
            "rows": json.dumps(prediction["rows"]),
            "response": "prediction complete",
            "graph_receipt": {"database_name": "alpha_predictions"},
        }
        PREDICTION_QUALIFIER._validate_prediction(answer, "alpha_predictions")
        self.assertEqual(
            ([{"id": 1, "score": 0.75}], 1), MCP_ADAPTER._safe_rows(answer)
        )

        with tempfile.TemporaryDirectory(
            prefix="query-claw-prediction-receipt-"
        ) as temporary:
            receipt = Path(temporary) / "prediction-qualified.json"
            receipt.write_text("stale\n", encoding="utf-8")
            receipt.chmod(0o644)
            PREDICTION_QUALIFIER._write_receipt(
                receipt, "a" * 64, ["alpha_predictions"]
            )

            self.assertEqual(0o600, stat.S_IMODE(receipt.stat().st_mode))
            self.assertEqual(
                {
                    "schema_version": 1,
                    "selection_fingerprint": "a" * 64,
                    "databases": ["alpha_predictions"],
                },
                json.loads(receipt.read_text(encoding="utf-8")),
            )


class QueryClawAdapterTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "test-query-claw-token-that-is-long-enough"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="query-claw-adapter-")
        self.manifest = Path(self.temporary.name) / "active-data-packs.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "fingerprint": "a" * 64,
                    "datasets": [
                        self.dataset("alpha"),
                        self.dataset("beta", views=("documents",)),
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def dataset(identifier: str, views=("structured", "documents", "predictions")):
        bindings = {}
        if {"structured", "predictions"} & set(views):
            bindings["ontology"] = {
                "database": f"{identifier}_records",
                "prediction_database": f"{identifier}_predictions",
            }
            if "predictions" in views:
                bindings["ontology"]["prediction_probe"] = (
                    f"Predict a supported outcome for {identifier}."
                )
        if "documents" in views:
            bindings["retriever"] = {"collection": f"{identifier}_documents"}
        return {
            "schema_version": 1,
            "id": identifier,
            "title": identifier.title(),
            "description": f"{identifier.title()} data",
            "industry": "Other",
            "views": {view: f"packs/{identifier}/{view}" for view in views},
            "bindings": bindings,
        }

    def server(self, gsf, retriever):
        with (
            patch.object(
                MCP_ADAPTER.httpx,
                "AsyncClient",
                side_effect=[gsf, retriever],
            ) as clients,
            patch.dict(
                MCP_ADAPTER.os.environ,
                {
                    "MCP_BEARER_TOKEN": self.TOKEN,
                    "QUERY_CLAW_ACTIVE_MANIFEST": str(self.manifest),
                    "RETRIEVER_API_TOKEN": self.TOKEN,
                },
                clear=True,
            ),
        ):
            server = MCP_ADAPTER.query_claw_server()
        self.client_timeouts = [
            call.kwargs["timeout"] for call in clients.call_args_list
        ]
        return server

    def readiness_server(self, database_names=()):
        status = MagicMock()
        status.json.return_value = {"calculated": True}
        databases = MagicMock()
        databases.json.return_value = {
            "data": [{"name": name} for name in database_names]
        }

        async def get(path):
            return status if path == "/api/semantic-compilation/status" else databases

        gsf = MagicMock(get=AsyncMock(side_effect=get), aclose=AsyncMock())
        collection = MagicMock()
        retriever = MagicMock(
            get=AsyncMock(return_value=collection), aclose=AsyncMock()
        )
        return self.server(gsf, retriever), gsf, retriever

    async def create_scope(self, server, datasets):
        request = types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.TOKEN}"},
            json=AsyncMock(return_value={"datasets": datasets, "ttl_seconds": 60}),
        )
        created = await server.routes["/scopes/create"](request)
        return created["content"]["scope_token"]

    def test_catalog_requires_an_explicit_active_dataset_without_disclosure(
        self,
    ) -> None:
        catalog = MCP_ADAPTER.DatasetCatalog(self.manifest)
        self.assertEqual(
            ["documents", "predictions", "records"],
            catalog.public_inventory()[0]["views"],
        )
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "dataset_id is required"):
            catalog.resolve("structured")
        for identifier in ("hidden-pack", "not safe"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(MCP_ADAPTER.ToolError) as raised:
                    catalog.resolve("structured", identifier)
                self.assertEqual("dataset is not active", str(raised.exception))
                self.assertNotIn("alpha", str(raised.exception))

        duplicate = Path(self.temporary.name) / "duplicate.json"
        duplicate.write_text(
            '{"schema_version":1,"schema_version":1,"fingerprint":"'
            + "a" * 64
            + '","datasets":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            MCP_ADAPTER.DatasetCatalog(duplicate)

        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["datasets"][1]["bindings"]["retriever"]["collection"] = value["datasets"][
            0
        ]["bindings"]["retriever"]["collection"]
        duplicate.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "share"):
            MCP_ADAPTER.DatasetCatalog(duplicate)

    async def test_scopes_only_narrow_and_audit_dataset_attempts(self) -> None:
        scopes = MCP_ADAPTER.ScopeStore(MCP_ADAPTER.DatasetCatalog(self.manifest))
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "source_scope is required"):
            await scopes.resolve(None, "structured", "alpha", "ask_question")
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "source_scope is required"):
            await scopes.public_inventory()
        token = await scopes.create([{"id": "alpha", "views": ["records"]}], 60)
        dataset = await scopes.resolve(token, "structured", "alpha", "ask_question")
        self.assertEqual("alpha", dataset.id)
        audit = await scopes.inspect(token)
        self.assertEqual(
            [{"tool": "ask_question", "dataset_id": "alpha"}],
            audit["calls"],
        )
        self.assertEqual("attempted", audit["call_semantics"])
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "outside this source scope"):
            await scopes.resolve(token, "documents", "beta", "query")
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "view is outside"):
            await scopes.resolve(token, "documents", "alpha", "query")
        revoked = await scopes.inspect(token, revoke=True)
        self.assertEqual(audit["calls"], revoked["calls"])
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "invalid or expired"):
            await scopes.resolve(token, "structured", "alpha", "ask_question")

    async def test_active_scope_blocks_single_dataset_unscoped_bypass(self) -> None:
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["datasets"] = value["datasets"][:1]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        scopes = MCP_ADAPTER.ScopeStore(MCP_ADAPTER.DatasetCatalog(self.manifest))

        dataset = await scopes.resolve(None, "structured", None, "ask_question")
        self.assertEqual("alpha", dataset.id)

        view_only = await scopes.create([{"id": "alpha", "views": ["documents"]}], 60)
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "source_scope is required"):
            await scopes.resolve(None, "structured", None, "ask_question")
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "source_scope is required"):
            await scopes.public_inventory()
        await scopes.inspect(view_only, revoke=True)
        self.assertEqual(
            "alpha",
            (await scopes.resolve(None, "structured", None, "ask_question")).id,
        )

        deny_all = await scopes.create([], 60)
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "source_scope is required"):
            await scopes.resolve(None, "structured", None, "ask_question")
        scopes._scopes[scopes._key(deny_all)].expires_at = MCP_ADAPTER.datetime.now(
            MCP_ADAPTER.UTC
        ) - MCP_ADAPTER.timedelta(seconds=1)
        self.assertEqual(
            "alpha",
            (await scopes.resolve(None, "structured", None, "ask_question")).id,
        )

    async def test_scope_control_routes_require_the_facade_bearer(self) -> None:
        gsf = MagicMock(aclose=AsyncMock())
        retriever = MagicMock(aclose=AsyncMock())
        routes = self.server(gsf, retriever).routes
        request = types.SimpleNamespace(headers={}, json=AsyncMock())
        denied = await routes["/scopes/create"](request)
        self.assertEqual(401, denied["status_code"])
        request.json.assert_not_awaited()

        request = types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.TOKEN}"},
            json=AsyncMock(
                return_value={
                    "datasets": [{"id": "alpha", "views": ["records"]}],
                    "ttl_seconds": 60,
                }
            ),
        )
        created = await routes["/scopes/create"](request)
        self.assertIn("scope_token", created["content"])

        request = types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.TOKEN}"},
            json=AsyncMock(return_value={"datasets": {"id": "alpha"}}),
        )
        malformed = await routes["/scopes/create"](request)
        self.assertEqual(400, malformed["status_code"])

        for datasets in (
            [{"id": [], "views": ["records"]}],
            [{"id": "alpha", "views": [{}]}],
        ):
            with self.subTest(datasets=datasets):
                request = types.SimpleNamespace(
                    headers={"authorization": f"Bearer {self.TOKEN}"},
                    json=AsyncMock(return_value={"datasets": datasets}),
                )
                malformed = await routes["/scopes/create"](request)
                self.assertEqual(400, malformed["status_code"])

    async def test_scoped_readiness_filters_dataset_inventory_without_recording(
        self,
    ) -> None:
        server, _gsf, _retriever = self.readiness_server(
            ("alpha_records", "alpha_predictions")
        )
        token = await self.create_scope(
            server,
            [
                {
                    "id": "alpha",
                    "views": ["records", "documents", "predictions"],
                }
            ],
        )

        readiness = await server.tools["check_readiness"](scope_token=token)

        self.assertNotIn("selection_fingerprint", readiness)
        self.assertEqual(["alpha"], [item["id"] for item in readiness["datasets"]])
        self.assertEqual(
            ["documents", "predictions", "records"],
            readiness["datasets"][0]["views"],
        )
        request = types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.TOKEN}"},
            json=AsyncMock(return_value={"scope_token": token}),
        )
        audit = await server.routes["/scopes/read"](request)
        self.assertEqual([], audit["content"]["calls"])
        self.assertEqual("attempted", audit["content"]["call_semantics"])

    async def test_scoped_readiness_filters_hidden_views(self) -> None:
        server, gsf, retriever = self.readiness_server()
        token = await self.create_scope(
            server, [{"id": "alpha", "views": ["documents"]}]
        )

        readiness = await server.tools["check_readiness"](scope_token=token)

        self.assertTrue(readiness["ready"])
        self.assertEqual(["documents"], readiness["datasets"][0]["views"])
        self.assertEqual({"documents": True}, readiness["datasets"][0]["readiness"])
        gsf.get.assert_not_awaited()
        retriever.get.assert_awaited_once_with("/v1/collections/alpha_documents")
        retriever.get.return_value.raise_for_status.assert_called_once_with()

    async def test_prediction_readiness_requires_a_matching_receipt(self) -> None:
        async def readiness() -> bool:
            server, _gsf, _retriever = self.readiness_server(
                ("alpha_records", "alpha_predictions")
            )
            token = await self.create_scope(
                server, [{"id": "alpha", "views": ["predictions"]}]
            )
            result = await server.tools["check_readiness"](scope_token=token)
            return result["datasets"][0]["readiness"]["predictions"]

        self.assertFalse(await readiness())
        receipt = self.manifest.parent / "prediction-qualified.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "selection_fingerprint": "b" * 64,
                    "databases": ["alpha_predictions"],
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(await readiness())
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "selection_fingerprint": "a" * 64,
                    "databases": ["alpha_predictions"],
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(await readiness())

    async def test_scoped_readiness_accepts_and_validates_dataset_id(self) -> None:
        server, _gsf, _retriever = self.readiness_server(("alpha_records",))
        token = await self.create_scope(
            server,
            [
                {"id": "alpha", "views": ["records"]},
                {"id": "beta", "views": ["documents"]},
            ],
        )

        readiness = await server.tools["check_readiness"](
            dataset_id="alpha", scope_token=token
        )

        self.assertEqual(["alpha"], [item["id"] for item in readiness["datasets"]])
        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "outside this source scope"):
            await server.tools["check_readiness"](
                dataset_id="hidden", scope_token=token
            )

    async def test_scoped_readiness_rejects_an_invalid_token_before_discovery(
        self,
    ) -> None:
        server, gsf, _retriever = self.readiness_server()

        with self.assertRaisesRegex(MCP_ADAPTER.ToolError, "invalid or expired"):
            await server.tools["check_readiness"](scope_token="invalid")

        gsf.get.assert_not_awaited()

    async def test_deny_all_scope_is_ready_but_denies_every_data_tool(self) -> None:
        server, gsf, _retriever = self.readiness_server()
        token = await self.create_scope(server, [])

        readiness = await server.tools["check_readiness"](scope_token=token)

        self.assertTrue(readiness["ready"])
        self.assertNotIn("selection_fingerprint", readiness)
        self.assertEqual([], readiness["datasets"])
        gsf.get.assert_not_awaited()
        for tool_name in ("check_answerable", "ask_question", "query", "predict"):
            with self.subTest(tool=tool_name):
                with self.assertRaisesRegex(
                    MCP_ADAPTER.ToolError, "no datasets are available"
                ):
                    await server.tools[tool_name](
                        "question", dataset_id="alpha", scope_token=token
                    )

    async def test_facade_forces_dataset_bindings_and_bounds_results(self) -> None:
        rows = [{"record_id": index} for index in range(30)]
        prediction_database = "alpha_predictions"

        class StreamResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                answer = {
                    "sql_code": "SELECT record_id FROM records",
                    "sql_response_from_db": rows,
                    "response": "bounded answer",
                    "graph_receipt": {"database_name": prediction_database},
                }
                yield "data: " + json.dumps({"type": "result", "answer": answer})

        gsf = MagicMock()
        gsf.stream.return_value = StreamResponse()
        gsf.aclose = AsyncMock()
        retriever_response = MagicMock()
        retriever_response.json.return_value = {
            "dataset_id": "attempted-overwrite",
            "results": [
                {
                    "evidence": [
                        {
                            "text": "notice",
                            "source": "doc-1",
                            "locator": {"kind": "page", "value": 1},
                        }
                    ]
                }
            ],
        }
        retriever = MagicMock()
        retriever.post = AsyncMock(return_value=retriever_response)
        retriever.aclose = AsyncMock()
        server = self.server(gsf, retriever)
        tools = server.tools
        self.assertEqual([110, 110], self.client_timeouts)
        self.assertEqual(
            {"check_readiness", "check_answerable", "ask_question", "query", "predict"},
            set(tools),
        )
        scope_token = await self.create_scope(
            server,
            [
                {"id": "alpha", "views": ["records", "predictions"]},
                {"id": "beta", "views": ["documents"]},
            ],
        )

        real_timeout = MCP_ADAPTER.asyncio.timeout
        with patch.object(
            MCP_ADAPTER.asyncio, "timeout", side_effect=real_timeout
        ) as total_timeout:
            structured = await tools["ask_question"](
                "List records", "alpha", scope_token=scope_token
            )
        total_timeout.assert_called_once_with(110)
        self.assertEqual("alpha", structured["dataset_id"])
        self.assertEqual(25, len(structured["rows"]))
        self.assertEqual(30, structured["row_count"])
        self.assertTrue(structured["truncated"])
        self.assertEqual(
            "alpha_records", gsf.stream.call_args.kwargs["json"]["target_db"]
        )

        documents = await tools["query"](
            "Find the notice", "beta", scope_token=scope_token, top_k=5
        )
        self.assertEqual("beta", documents["dataset_id"])
        self.assertEqual("notice", documents["results"][0]["evidence"][0]["text"])
        retriever.post.assert_awaited_once_with(
            "/v1/query",
            json={
                "query": "Find the notice",
                "top_k": 5,
                "format": "evidence",
                "rerank": False,
                "collection_name": "beta_documents",
            },
        )

        prediction = await tools["predict"](
            "What happens next?", "alpha", scope_token=scope_token
        )
        payload = gsf.stream.call_args.kwargs["json"]
        self.assertEqual(
            {
                "question": "What happens next?",
                "prediction": True,
                "target_db": "alpha_predictions",
            },
            payload,
        )
        self.assertEqual("alpha", prediction["dataset_id"])

        prediction_database = "beta_predictions"
        with self.assertRaisesRegex(
            MCP_ADAPTER.ToolError, "prediction source did not match"
        ):
            await tools["predict"](
                "What happens next?", "alpha", scope_token=scope_token
            )

    async def test_upstream_errors_are_redacted(self) -> None:
        secret = "private-upstream-host-and-secret"

        class ErrorStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "data: " + json.dumps({"type": "error", "message": secret})

        gsf = MagicMock()
        gsf.stream.return_value = ErrorStream()
        gsf.aclose = AsyncMock()
        retriever_response = MagicMock()
        retriever_response.raise_for_status.side_effect = MCP_ADAPTER.httpx.HTTPError(
            secret
        )
        retriever = MagicMock()
        retriever.post = AsyncMock(return_value=retriever_response)
        retriever.aclose = AsyncMock()
        server = self.server(gsf, retriever)
        tools = server.tools
        scope_token = await self.create_scope(
            server,
            [
                {"id": "alpha", "views": ["records"]},
                {"id": "beta", "views": ["documents"]},
            ],
        )

        with self.assertRaises(MCP_ADAPTER.ToolError) as ontology:
            await tools["ask_question"]("question", "alpha", scope_token=scope_token)
        self.assertEqual("Ontology agent failed", str(ontology.exception))
        with self.assertRaises(MCP_ADAPTER.ToolError) as documents:
            await tools["query"]("question", "beta", scope_token=scope_token)
        self.assertEqual("Retriever query failed", str(documents.exception))
        self.assertNotIn(secret, str(ontology.exception) + str(documents.exception))

        request = types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.TOKEN}"},
            json=AsyncMock(return_value={"scope_token": scope_token}),
        )
        audit = await server.routes["/scopes/read"](request)
        self.assertEqual(
            [
                {"tool": "ask_question", "dataset_id": "alpha"},
                {"tool": "query", "dataset_id": "beta"},
            ],
            audit["content"]["calls"],
        )
        self.assertEqual("attempted", audit["content"]["call_semantics"])


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
                    LIVE_EVALUATOR.tool("ontology", "check_answerable"),
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
                        (LIVE_EVALUATOR.tool("ontology", "ask_question"),),
                        answer,
                        response_class="answer",
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
