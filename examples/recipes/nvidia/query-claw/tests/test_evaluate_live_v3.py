# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = EXAMPLE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_evaluator():
    path = SCRIPTS / "evaluate_live.py"
    spec = importlib.util.spec_from_file_location("query_claw_evaluate_live_v3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVALUATOR = _load_evaluator()


def _case(
    *,
    case_id: str = "alpha-case",
    session: str = "",
    turn: int = 1,
    routes: list[str] | None = None,
    forbidden_routes: list[str] | None = None,
    response: list[str] | None = None,
    views: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 3,
        "id": case_id,
        "origin_id": f"origin::{case_id}",
        "cohort": "structured",
        "session": session,
        "turn": turn,
        "prompt": "Find the supported record.",
        "sources": ["alpha_structured"],
        "datasets": [{"id": "alpha", "views": views or ["records", "documents"]}],
        "expected": {
            "routes": routes or ["ontology"],
            "forbidden_routes": forbidden_routes or ["kumo"],
            "route_only": True,
            "response": response or ["answer"],
            "facts": ["stable fact", "alternate fact"],
            "facts_policy": "any",
            "forbidden_facts": ["wrong dataset"],
            "citations": ["REC-1"],
        },
    }


def _events(tools: list[str], output: str) -> list[dict]:
    events: list[dict] = []
    for name in tools:
        events.extend(
            [
                {"event": "tool.started", "tool": name},
                {"event": "tool.completed", "tool": name, "error": False},
            ]
        )
    events.append({"event": "run.completed", "output": output})
    return events


class EvaluateLiveV3Tests(unittest.TestCase):
    def test_no_available_dataset_response_is_an_abstention(self) -> None:
        answer = (
            "No dataset is currently available, so I can’t retrieve the documented "
            "readiness risks. Sources used: none."
        )
        self.assertEqual(
            "abstention",
            EVALUATOR.classify_response(
                answer, {"status": "completed", "output": answer}
            ),
        )
        raw = _case(response=["abstention"], views=["records"])
        raw["datasets"] = []
        raw["expected"]["routes"] = []
        _, case = EVALUATOR.parse_suite_case(raw, "fixture")
        self.assertEqual(
            answer,
            EVALUATOR.validate_answer(
                case, answer, {"status": "completed", "output": answer}
            ),
        )

        existential = (
            "There are no datasets available, so I cannot retrieve evidence. "
            "Would you like to configure one?"
        )
        self.assertEqual(
            "abstention",
            EVALUATOR.classify_response(
                existential, {"status": "completed", "output": existential}
            ),
        )

    def test_only_concise_information_requests_are_clarifications(self) -> None:
        self.assertEqual(
            {"answer", "abstention"}, EVALUATOR.JUDGED_RESPONSE_CLASSES
        )
        clarification = "Which dataset should I use?"
        self.assertEqual(
            "clarification",
            EVALUATOR.classify_response(
                clarification, {"status": "completed", "output": clarification}
            ),
        )

        answer = " ".join(
            ["Query Claw can inspect governed records and cited documents."] * 10
        ) + " Which source would you like to explore?"
        self.assertGreater(len(answer.split()), EVALUATOR.MAX_CLARIFICATION_WORDS)
        self.assertEqual(
            "answer",
            EVALUATOR.classify_response(
                answer, {"status": "completed", "output": answer}
            ),
        )

    def test_source_issued_qualifier_does_not_make_an_answer_an_abstention(
        self,
    ) -> None:
        answer = (
            "The risk levels are analyst interpretations of the documented dates and "
            "blocker counts—not source-issued risk ratings."
        )
        self.assertEqual(
            "answer",
            EVALUATOR.classify_response(
                answer, {"status": "completed", "output": answer}
            ),
        )

    def test_unavailable_view_responses_are_abstentions(self) -> None:
        answers = (
            "Structured telemetry is unavailable, so I can't calculate headroom.",
            "The selected dataset has no documents, so I can't retrieve the brief.",
            "That evidence is unavailable, so I can't verify the reported cause.",
        )
        for answer in answers:
            with self.subTest(answer=answer):
                self.assertEqual(
                    "abstention",
                    EVALUATOR.classify_response(
                        answer, {"status": "completed", "output": answer}
                    ),
                )

    def test_v3_route_response_and_evidence_contract(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        readiness = EVALUATOR.tool("ontology", "check_readiness")
        ontology = EVALUATOR.tool("ontology", "ask_question")
        retriever = EVALUATOR.tool("retriever", "query")
        answer = "Alternate fact is supported by REC-1."
        self.assertEqual(
            [readiness, ontology],
            EVALUATOR.validate_run(
                case,
                _events([readiness, ontology], answer),
                {"status": "completed", "output": answer},
            ),
        )
        self.assertNotIn(retriever, case.allowed_query_tools)

        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "unexpected routes"):
            EVALUATOR.validate_run(
                case,
                _events([ontology, retriever], answer),
                {"status": "completed", "output": answer},
                prevalidated=True,
            )

        for output, message in (
            ("No matching evidence at REC-1.", "deterministic evidence"),
            ("Stable fact, wrong dataset, REC-1.", "forbidden fact"),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(EVALUATOR.EvaluationError, message),
            ):
                EVALUATOR.validate_run(
                    case,
                    _events([ontology], output),
                    {"status": "completed", "output": output},
                )

        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "required routes"):
            EVALUATOR.validate_run(
                case,
                _events(
                    [
                        readiness,
                        EVALUATOR.tool("ontology", "check_answerable"),
                    ],
                    answer,
                ),
                {"status": "completed", "output": answer},
            )

        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "out-of-contract MCP"):
            EVALUATOR.validate_run(
                case,
                _events([EVALUATOR.tool("kumo", "predict")], answer),
                {"status": "completed", "output": answer},
            )

    def test_tool_failures_and_call_bounds_precede_answer_checks(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        ontology = EVALUATOR.tool("ontology", "ask_question")
        incomplete = "REC-1"

        failed = [
            {"event": "tool.started", "tool": ontology},
            {"event": "tool.failed", "tool": ontology},
            {"event": "run.completed", "output": incomplete},
        ]
        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "tool.failed"):
            EVALUATOR.validate_run(
                case,
                failed,
                {"status": "completed", "output": incomplete},
            )

        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "call bound"):
            EVALUATOR.validate_run(
                case,
                _events([ontology, ontology, ontology], incomplete),
                {"status": "completed", "output": incomplete},
            )

    def test_v3_response_classes_and_error_are_supported(self) -> None:
        raw = _case(routes=[], forbidden_routes=["ontology", "retriever", "kumo"])
        raw["datasets"] = []
        raw["sources"] = []
        raw["expected"] = {
            "routes": [],
            "forbidden_routes": ["ontology", "retriever", "kumo"],
            "route_only": True,
            "response": ["clarification"],
        }
        _, clarification = EVALUATOR.parse_suite_case(raw, "fixture")
        answer = "Which dataset should I use?\n\nSources used: none"
        EVALUATOR.validate_run(
            clarification,
            _events([], answer),
            {"status": "completed", "output": answer},
        )

        raw["expected"]["response"] = ["error"]
        _, error_case = EVALUATOR.parse_suite_case(raw, "fixture")
        EVALUATOR.validate_run(
            error_case,
            [{"event": "run.failed"}],
            {"status": "failed", "output": ""},
        )

    def test_jsonl_requires_consistent_v3_markers_and_turn_order(self) -> None:
        first = _case(case_id="first", session="switch", turn=1)
        second = _case(case_id="second", session="switch", turn=3)
        with tempfile.TemporaryDirectory(prefix="query-claw-v3-") as directory:
            path = Path(directory) / "suite.jsonl"
            path.write_text(
                "\n".join(json.dumps(case) for case in (first, second)) + "\n",
                encoding="utf-8",
            )
            _name, parsed = EVALUATOR.load_suite(path)
            self.assertEqual([1, 3], [case.turn for _id, case in parsed])

            unmarked = dict(second)
            unmarked.pop("schema_version")
            path.write_text(
                "\n".join(json.dumps(case) for case in (first, unmarked)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EVALUATOR.EvaluationError, "must not mix"):
                EVALUATOR.load_suite(path)

            second["turn"] = 1
            path.write_text(
                "\n".join(json.dumps(case) for case in (first, second)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EVALUATOR.EvaluationError, "increasing"):
                EVALUATOR.load_suite(path)

    def test_named_session_rotates_when_its_dataset_grant_changes(self) -> None:
        cases = []
        for case_id, turn, views, routes in (
            ("records-one", 1, ["records"], ["ontology"]),
            ("records-two", 2, ["records"], ["ontology"]),
            ("documents-one", 3, ["documents"], ["retriever"]),
            ("documents-two", 4, ["documents"], ["retriever"]),
        ):
            raw = _case(
                case_id=case_id,
                session="logical-switch",
                turn=turn,
                views=views,
                routes=routes,
            )
            raw["expected"]["forbidden_routes"] = [
                route
                for route in ("ontology", "retriever", "kumo")
                if route not in routes
            ]
            cases.append(EVALUATOR.parse_suite_case(raw, "fixture"))

        planned = EVALUATOR.plan_sessions(cases)
        self.assertEqual(planned[0][2], planned[1][2])
        self.assertNotEqual(planned[1][2], planned[2][2])
        self.assertEqual(planned[2][2], planned[3][2])
        self.assertEqual([False, True, False, True], [item[3] for item in planned])
        self.assertEqual(
            [("logical-switch", turn) for turn in range(1, 5)],
            [(item[1].session, item[1].turn) for item in planned],
        )

    def test_scope_client_sends_dataset_views_and_reads_audit(self) -> None:
        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "HTTPS"):
            EVALUATOR.SourceScopeClient("http://facade.example/query-claw", "x" * 32)
        for invalid in (
            "https://facade.example",
            "https://facade.example/mcp/",
            "https://facade.example/query-claw/scopes",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(EVALUATOR.EvaluationError, "/query-claw"),
            ):
                EVALUATOR.SourceScopeClient(invalid, "x" * 32)
        EVALUATOR.SourceScopeClient("http://127.0.0.1:8000/query-claw", "x" * 32)
        client = EVALUATOR.SourceScopeClient(
            "https://facade.example/query-claw/", "x" * 32
        )
        self.assertEqual("https://facade.example/query-claw", client.base_url)
        client._request = MagicMock(
            side_effect=[
                {"scope_token": "s" * 32},
                {"calls": [{"tool": "ask_question", "dataset_id": "alpha"}]},
                {"calls": [{"tool": "ask_question", "dataset_id": "alpha"}]},
            ]
        )
        datasets = (EVALUATOR.DatasetSelection("alpha", ("records",)),)
        token = client.create(datasets, 60)
        calls = client.read(token)
        self.assertEqual(calls, client.revoke(token))
        self.assertEqual(
            {
                "datasets": [{"id": "alpha", "views": ["records"]}],
                "ttl_seconds": 60,
            },
            client._request.call_args_list[0].args[1],
        )
        client._request = MagicMock(return_value={"scope_token": "z" * 32})
        client.create((), 60)
        self.assertEqual([], client._request.call_args.args[1]["datasets"])

    def test_scoped_run_revokes_each_turn_and_rejects_wrong_view(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(views=["records"]), "fixture")
        with self.assertRaisesRegex(EVALUATOR.EvaluationError, "source-scope facade"):
            EVALUATOR.run_case_with_scope(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                session_id="session",
                delete_after=False,
                scope_client=None,
                scope_ttl=90,
            )

        client = MagicMock()
        client.create.return_value = "s" * 32
        calls = (("check_readiness", "alpha"), ("ask_question", "alpha"))
        client.read.return_value = calls
        client.revoke.return_value = calls
        with patch.object(
            EVALUATOR,
            "run_case",
            return_value=EVALUATOR.RunResult(
                (EVALUATOR.tool("ontology", "ask_question"),),
                "Stable fact REC-1",
                response_class="answer",
            ),
        ) as run:
            result = EVALUATOR.run_case_with_scope(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                session_id="session",
                delete_after=False,
                scope_client=client,
                scope_ttl=90,
            )
        self.assertEqual(calls, result.source_calls)
        self.assertEqual("s" * 32, run.call_args.kwargs["scope_token"])
        client.read.assert_called_once()
        client.revoke.assert_called_once()

        client.read.return_value = (
            ("check_readiness", "alpha"),
            ("query", "alpha"),
        )
        client.revoke.return_value = client.read.return_value
        with (
            patch.object(
                EVALUATOR,
                "run_case",
                return_value=EVALUATOR.RunResult(
                    (EVALUATOR.tool("ontology", "ask_question"),),
                    "Stable fact REC-1",
                    response_class="answer",
                ),
            ),
            self.assertRaisesRegex(EVALUATOR.EvaluationError, "view outside"),
        ):
            EVALUATOR.run_case_with_scope(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                session_id="session",
                delete_after=False,
                scope_client=client,
                scope_ttl=90,
            )

    def test_scope_token_is_instruction_only_and_leak_preserves_answer(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        ontology = EVALUATOR.tool("ontology", "ask_question")
        token = "scope-secret-" + "x" * 32
        clean = "Stable fact REC-1"
        with (
            patch.object(
                EVALUATOR,
                "request_json",
                side_effect=[
                    (202, {"run_id": "run"}),
                    (200, {"status": "completed", "output": clean}),
                    (200, {"deleted": True}),
                ],
            ) as request,
            patch.object(
                EVALUATOR,
                "stream_events",
                return_value=iter(_events([ontology], clean)),
            ),
        ):
            EVALUATOR.run_case(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                scope_token=token,
            )
        payload = request.call_args_list[0].kwargs["payload"]
        self.assertNotIn(token, payload["input"])
        self.assertIn(token, payload["instructions"])
        self.assertIn("alpha (views: records, documents)", payload["instructions"])

        leaked = f"Stable fact REC-1 {token}"
        with (
            patch.object(
                EVALUATOR,
                "request_json",
                side_effect=[
                    (202, {"run_id": "run"}),
                    (200, {"status": "completed", "output": leaked}),
                ],
            ),
            patch.object(
                EVALUATOR,
                "stream_events",
                return_value=iter(_events([ontology], leaked)),
            ),
            patch.object(EVALUATOR, "stop_run"),
            patch.object(EVALUATOR, "delete_session_best_effort"),
            self.assertRaisesRegex(EVALUATOR.EvaluationError, "leaked") as raised,
        ):
            EVALUATOR.run_case(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                scope_token=token,
            )
        self.assertEqual(
            "Stable fact REC-1 [REDACTED_SOURCE_SCOPE]",
            raised.exception.result.answer,
        )

    def test_rejected_tool_start_is_retained_in_the_result(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        rejected = EVALUATOR.tool("kumo", "predict")
        with (
            patch.object(
                EVALUATOR,
                "request_json",
                return_value=(202, {"run_id": "run"}),
            ),
            patch.object(
                EVALUATOR,
                "stream_events",
                return_value=iter([{"event": "tool.started", "tool": rejected}]),
            ),
            patch.object(EVALUATOR, "stop_run"),
            patch.object(EVALUATOR, "delete_session_best_effort"),
            self.assertRaisesRegex(
                EVALUATOR.EvaluationError, "out-of-contract MCP"
            ) as raised,
        ):
            EVALUATOR.run_case("alpha", case, "http://127.0.0.1:8642", "key", 10)

        self.assertEqual((rejected,), raised.exception.result.tools)
        self.assertEqual((), raised.exception.result.successful_tools)

    def test_scope_is_inspected_and_revoked_after_an_unexpected_run_error(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        client = MagicMock()
        client.create.return_value = "s" * 32
        client.read.return_value = ()
        client.revoke.return_value = ()
        with (
            patch.object(EVALUATOR, "run_case", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            EVALUATOR.run_case_with_scope(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                session_id="session",
                delete_after=False,
                scope_client=client,
                scope_ttl=90,
            )

        client.read.assert_called_once_with("s" * 32)
        client.revoke.assert_called_once_with("s" * 32)

    def test_unexpected_scope_cleanup_errors_are_bounded_and_revoke_is_attempted(
        self,
    ) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        calls = (("ask_question", "alpha"),)
        for failing_method, expected_error in (
            ("read", "audit read failed: RuntimeError"),
            ("revoke", "revoke failed: RuntimeError"),
        ):
            client = MagicMock()
            client.create.return_value = "s" * 32
            client.read.return_value = calls
            client.revoke.return_value = calls
            getattr(client, failing_method).side_effect = RuntimeError("boom")
            with (
                self.subTest(failing_method=failing_method),
                patch.object(
                    EVALUATOR,
                    "run_case",
                    return_value=EVALUATOR.RunResult((), "answer"),
                ),
                self.assertRaisesRegex(EVALUATOR.EvaluationError, expected_error),
            ):
                EVALUATOR.run_case_with_scope(
                    "alpha",
                    case,
                    "http://127.0.0.1:8642",
                    "key",
                    10,
                    session_id="session",
                    delete_after=False,
                    scope_client=client,
                    scope_ttl=90,
                )
            client.revoke.assert_called_once_with("s" * 32)

    def test_scope_cleanup_failure_does_not_hide_the_primary_failure(self) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        client = MagicMock()
        client.create.return_value = "s" * 32
        client.read.side_effect = RuntimeError("read boom")
        client.revoke.side_effect = RuntimeError("revoke boom")
        primary = EVALUATOR.EvaluationError("primary route failure")
        with (
            patch.object(EVALUATOR, "run_case", side_effect=primary),
            self.assertRaisesRegex(EVALUATOR.EvaluationError, "primary route failure"),
        ):
            EVALUATOR.run_case_with_scope(
                "alpha",
                case,
                "http://127.0.0.1:8642",
                "key",
                10,
                session_id="session",
                delete_after=False,
                scope_client=client,
                scope_ttl=90,
            )
        client.revoke.assert_called_once_with("s" * 32)

    def test_validation_failure_preserves_answer_and_detailed_output_is_private(
        self,
    ) -> None:
        _, case = EVALUATOR.parse_suite_case(_case(), "fixture")
        ontology = EVALUATOR.tool("ontology", "ask_question")
        answer = "REC-1 without the required fact"
        with (
            patch.object(
                EVALUATOR,
                "request_json",
                side_effect=[
                    (202, {"run_id": "run"}),
                    (200, {"status": "completed", "output": answer}),
                ],
            ),
            patch.object(
                EVALUATOR,
                "stream_events",
                return_value=iter(_events([ontology], answer)),
            ),
            patch.object(EVALUATOR, "stop_run"),
            patch.object(EVALUATOR, "delete_session_best_effort"),
            self.assertRaises(EVALUATOR.EvaluationError) as raised,
        ):
            EVALUATOR.run_case("alpha", case, "http://127.0.0.1:8642", "key", 10)
        self.assertEqual(answer, raised.exception.result.answer)

        summary = {"passed": False, "latency_seconds": 1.25, "error": "failed"}
        record = EVALUATOR.detailed_result(
            "alpha",
            case,
            EVALUATOR.RunResult(
                (ontology, EVALUATOR.tool("retriever", "query")),
                answer,
                (("ask_question", "alpha"),),
                "answer",
                (ontology,),
            ),
            summary,
        )
        self.assertEqual(2, record["schema_version"])
        self.assertEqual(["ontology", "retriever"], record["attempted_routes"])
        self.assertEqual(["ontology"], record["successful_routes"])
        self.assertEqual(record["successful_routes"], record["actual_routes"])
        self.assertEqual(
            [{"tool": "ask_question", "dataset_id": "alpha"}],
            record["audited_dataset_attempts"],
        )
        self.assertEqual([ontology], record["successful_tools"])
        with tempfile.TemporaryDirectory(prefix="query-claw-detail-") as directory:
            output = Path(directory) / "detail.jsonl"
            EVALUATOR.prepare_private_output(output)
            EVALUATOR.append_result(output, record)
            self.assertEqual(0o600, os.stat(output).st_mode & 0o777)
            self.assertEqual(answer, json.loads(output.read_text())["answer"])
            with self.assertRaises(EVALUATOR.EvaluationError):
                EVALUATOR.prepare_private_output(output)


if __name__ == "__main__":
    unittest.main()
