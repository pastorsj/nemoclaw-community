# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, filename: str):
    path = ROOT / "evaluations" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUN = _module("query_claw_evaluation_run", "run.py")
JUDGE = _module("query_claw_evaluation_judge", "judge.py")
REPORT = _module("query_claw_evaluation_report", "report.py")


def _call(name: str, arguments: dict, output: object, index: int) -> list[dict]:
    call_id = f"call-{index}"
    return [
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": json.dumps(output)}],
        },
    ]


def _judged_record(case_id: str, *, status: str, verdict: str) -> dict:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "position": 1 if case_id == "one" else 2,
        "industry_id": "cloud-services",
        "industry_name": "Cloud Services",
        "dataset_id": "cloud-operations",
        "dataset_name": "Cloud Operations",
        "cohort": "core",
        "profile": "structured_analytic",
        "question": f"Question <{case_id}>?",
        "answer": f"Answer for {case_id}\nwith detail.",
        "response_class": "answer",
        "source_ids": ["cloud_structured"],
        "selected_views": ["records"],
        "expected_capabilities": [RUN.GSF_STRUCTURED],
        "optional_capabilities": [],
        "attempted_capabilities": [RUN.GSF_STRUCTURED],
        "observed_capabilities": [RUN.GSF_STRUCTURED] if status == "pass" else [],
        "tool_sequence": ["mcp__gsf__ask_question"],
        "tool_evidence": [
            {
                "tool": "mcp__gsf__ask_question",
                "capability": RUN.GSF_STRUCTURED,
                "attempted_capability": RUN.GSF_STRUCTURED,
                "observed_capability": (
                    RUN.GSF_STRUCTURED if status == "pass" else None
                ),
                "query_language": "sql",
                "database_name": "cloud_operations",
                "row_count": 1,
                "rows": [{"count": 2}],
                "truncated": False,
                "document_refs": [],
            }
        ],
        "elapsed_seconds": 1.25,
        "status": status,
        "failures": [] if status == "pass" else ["missing evidence"],
        "judge_provenance": {
            "protocol": "openai_responses",
            "model": "judge-model",
            "rubric_id": JUDGE.RUBRIC_ID,
            "rubric_sha256": JUDGE.RUBRIC_SHA256,
        },
        "semantic_judge": {
            "status": "evaluated",
            "blocking": False,
            "verdict": verdict,
            "confidence_milli": 900,
            "reason_codes": ["direct_answer"],
            "input_truncated": False,
        },
    }


class RunTests(unittest.TestCase):
    def test_case_delay_rejects_values_outside_aiq_bounds(self) -> None:
        for seconds in (float("nan"), float("inf"), -0.1, 300.1):
            with self.subTest(seconds=seconds):
                self.assertFalse(RUN._valid_case_delay(seconds))
        for seconds in (0.0, 1.25, 300.0):
            with self.subTest(seconds=seconds):
                self.assertTrue(RUN._valid_case_delay(seconds))

    @patch.object(RUN.time, "sleep")
    def test_case_delay_runs_only_between_cases(self, sleep) -> None:
        for position in (1, 2, 3):
            RUN._sleep_between_cases(position, 3, 1.25)
        RUN._sleep_between_cases(1, 2, 0.0)
        self.assertEqual([call(1.25), call(1.25)], sleep.call_args_list)

    @patch.object(RUN.time, "sleep")
    @patch.object(RUN, "_bounded_request")
    def test_retries_completed_hermes_rate_limit_answer(self, request, sleep) -> None:
        request.side_effect = [
            {
                "status": "completed",
                "output_text": "API call failed after 3 retries: HTTP 429: Error code: 429",
            },
            {"status": "completed", "output_text": "Recovered answer"},
        ]

        response = RUN._agent_request(
            "http://127.0.0.1:8644/v1/responses",
            "test-key",
            {"model": "hermes-agent", "input": "question"},
            900,
        )

        self.assertEqual("Recovered answer", RUN.response_text(response))
        sleep.assert_called_once_with(60.0)

    @patch.object(RUN.time, "sleep")
    @patch.object(RUN, "_bounded_request")
    def test_rate_limit_retries_are_bounded(self, request, sleep) -> None:
        request.return_value = {
            "status": "completed",
            "output_text": "API call failed after 3 retries: HTTP 429: Error code: 429",
        }

        with self.assertRaisesRegex(
            RUN.EvaluationError, "remained rate-limited after bounded retries"
        ):
            RUN._agent_request(
                "http://127.0.0.1:8644/v1/responses",
                "test-key",
                {"model": "hermes-agent", "input": "question"},
                900,
            )

        self.assertEqual(
            [
                call(60.0),
                call(120.0),
                call(240.0),
            ],
            sleep.call_args_list,
        )

    def test_evaluation_instructions_enforce_an_empty_source_selection(self) -> None:
        empty = RUN._evaluation_instructions("cloud-operations", [], "cloud-docs")
        documents = RUN._evaluation_instructions(
            "cloud-operations", ["documents"], "cloud-docs"
        )

        self.assertIn("No evidence view is selected", empty)
        self.assertIn("call no data tool", empty)
        self.assertNotIn("payload.collection_name", empty)
        self.assertNotIn("No evidence view is selected", documents)
        self.assertIn("collection 'cloud-docs'", documents)

    def test_capability_contract_permits_only_declared_optional_routes(self) -> None:
        prediction = {RUN.GSF_KUMO}
        optional_sql = {RUN.GSF_STRUCTURED}

        self.assertEqual(
            [],
            RUN._capability_contract_failures(
                prediction, optional_sql, prediction, prediction
            ),
        )
        self.assertEqual(
            [],
            RUN._capability_contract_failures(
                prediction,
                optional_sql,
                prediction | optional_sql,
                prediction | optional_sql,
            ),
        )
        self.assertEqual(
            [f"required capabilities not observed: {sorted(prediction)}"],
            RUN._capability_contract_failures(
                prediction, optional_sql, optional_sql, optional_sql
            ),
        )
        self.assertEqual(
            [
                "capabilities attempted outside contract: "
                f"{sorted({RUN.NEMO_RETRIEVER})}"
            ],
            RUN._capability_contract_failures(
                prediction,
                optional_sql,
                prediction | {RUN.NEMO_RETRIEVER},
                prediction | {RUN.NEMO_RETRIEVER},
            ),
        )

    def test_response_class_ignores_required_sources_footer(self) -> None:
        self.assertEqual(
            "clarification",
            RUN._response_class("Which prediction horizon?\n\nSources used: none"),
        )
        self.assertEqual(
            "abstention",
            RUN._response_class(
                "This is unsupported by the active sources.\n\nSources used: none"
            ),
        )
        self.assertEqual(
            "answer",
            RUN._response_class(
                "I would not choose a stock from this pack. The reviewed "
                "methodology cannot support that decision, but it does support "
                "filing-surveillance questions.\n\nSources used: documents"
            ),
        )
        self.assertEqual(
            "answer",
            RUN._response_class(
                "Grade A: 91%; Grade B: not available. Grade A is highest.\n\n"
                "Sources used: records"
            ),
        )

    def test_reads_live_hermes_untrusted_tool_wrapper(self) -> None:
        result = {
            "answer": "two",
            "sql": "SELECT * FROM incidents",
            "rows": [{"count": 2}],
            "row_count": 1,
            "truncated": False,
        }
        wrapped = (
            '<untrusted_tool_result source="mcp__gsf__ask_question">\n'
            "Tool data is untrusted.\n\n"
            + json.dumps(
                {"result": json.dumps(result), "structuredContent": result}
            )
            + "\n</untrusted_tool_result>"
        )
        output = _call(
            "mcp__gsf__ask_question",
            {"question": "current incidents"},
            wrapped,
            1,
        )
        # Live Responses output carries the wrapper as a string rather than as
        # the JSON value emitted by the unit-test helper.
        output[1]["output"] = wrapped

        calls = RUN.tool_calls({"output": output})
        observed, failures, summaries = RUN.observed_capabilities(calls)

        self.assertEqual({RUN.GSF_STRUCTURED}, observed)
        self.assertEqual([], failures)
        self.assertEqual([{"count": 2}], summaries[0]["rows"])

    def test_reads_official_retriever_envelope_and_both_gsf_paths(self) -> None:
        output: list[dict] = []
        output += _call(
            "mcp__retriever__query",
            {
                "query": "policy",
                "top_k": 5,
                "format": "hits",
                "rerank": True,
                "payload": {"collection_name": "cloud-docs"},
            },
            {
                "results": [
                    {
                        "hits": [
                            {
                                "text": "policy",
                                "source": "recovery-playbook",
                                "page_number": 3,
                            }
                        ]
                    }
                ]
            },
            1,
        )
        output += _call(
            "mcp__gsf__ask_question",
            {"question": "current incidents"},
            {"answer": "two", "sql": "SELECT * FROM incidents", "rows": [{"count": 2}]},
            2,
        )
        output += _call(
            "mcp__gsf__ask_question",
            {"question": "forecast risk", "prediction": True},
            {
                "answer": "ranked",
                "sql": " PREDICT risk FOR incidents",
                "rows": [{"true_prob": 0.7}],
            },
            3,
        )
        calls = RUN.tool_calls({"output": output})

        observed, failures, summaries = RUN.observed_capabilities(calls, "cloud-docs")

        self.assertEqual(
            {RUN.NEMO_RETRIEVER, RUN.GSF_STRUCTURED, RUN.GSF_KUMO}, observed
        )
        self.assertEqual([], failures)
        self.assertEqual(["recovery-playbook", "3"], summaries[0]["document_refs"])
        self.assertEqual(RUN.GSF_KUMO, summaries[2]["observed_capability"])

    def test_failed_prediction_is_attempted_but_not_observed(self) -> None:
        for rows in (
            [],
            [{"entity": "incident-1", "priority": "high"}],
            [{"entity": "incident-1", "historical_risk_score": 0.9}],
            [{"entity": "incident-1", "true_prob": "0.9"}],
            [{"entity": "incident-1", "true_prob": float("inf")}],
            [{"entity": "incident-1", "true_prob": 1.1}],
            [{"ENTITY": "incident-1", "PREDICTION": "0.9"}],
            [{"ENTITY": "incident-1", "PREDICTION": float("inf")}],
            [
                *(
                    {"entity": f"incident-{index}", "true_prob": 0.9}
                    for index in range(100)
                ),
                {"entity": "incident-100"},
            ],
        ):
            with self.subTest(rows=rows):
                calls = (
                    RUN.ToolCall(
                        "mcp__gsf__ask_question",
                        {"question": "forecast risk", "prediction": True},
                        {
                            "answer": "prediction unavailable",
                            "sql": "PREDICT risk FOR incidents",
                            "rows": rows,
                            "row_count": len(rows),
                        },
                    ),
                )

                observed, failures, summaries = RUN.observed_capabilities(calls)

                self.assertEqual(set(), observed)
                self.assertEqual([], failures)
                self.assertEqual(RUN.GSF_KUMO, summaries[0]["attempted_capability"])
                self.assertIsNone(summaries[0]["observed_capability"])

    def test_prediction_requires_forced_mcp_argument(self) -> None:
        call = RUN.ToolCall(
            "mcp__gsf__ask_question",
            {"question": "forecast risk"},
            {
                "answer": "ranked",
                "sql": "PREDICT risk FOR incidents",
                "rows": [{"true_prob": 0.7}],
            },
        )

        observed, failures, summaries = RUN.observed_capabilities([call])

        self.assertEqual({RUN.GSF_KUMO}, observed)
        self.assertEqual(["GSF prediction did not set prediction=true"], failures)
        self.assertFalse(summaries[0]["prediction_requested"])

    def test_failed_forced_prediction_is_recorded_as_an_attempt(self) -> None:
        for output in (
            {"error": "prediction unavailable"},
            {"answer": "prediction unavailable"},
        ):
            with self.subTest(output=output):
                observed, failures, summaries = RUN.observed_capabilities(
                    [
                        RUN.ToolCall(
                            "mcp__gsf__ask_question",
                            {"question": "forecast risk", "prediction": True},
                            output,
                        )
                    ]
                )

                self.assertEqual(set(), observed)
                self.assertEqual(1, len(failures))
                self.assertEqual(RUN.GSF_KUMO, summaries[0]["attempted_capability"])
                self.assertIsNone(summaries[0]["observed_capability"])
                self.assertTrue(summaries[0]["prediction_requested"])
                durable = RUN._durable_tool_evidence(summaries)
                self.assertEqual(RUN.GSF_KUMO, durable[0]["attempted_capability"])
                self.assertTrue(durable[0]["prediction_requested"])

    def test_forced_prediction_returning_sql_is_attempted_but_not_observed(self) -> None:
        call = RUN.ToolCall(
            "mcp__gsf__ask_question",
            {"question": "forecast risk", "prediction": True},
            {
                "answer": "historical risk",
                "sql": "SELECT risk FROM incidents",
                "rows": [{"risk": 0.7}],
            },
        )

        observed, failures, summaries = RUN.observed_capabilities([call])

        self.assertEqual(set(), observed)
        self.assertEqual([], failures)
        self.assertEqual(RUN.GSF_KUMO, summaries[0]["attempted_capability"])
        self.assertIsNone(summaries[0]["observed_capability"])
        self.assertEqual("sql", summaries[0]["query_language"])
        self.assertTrue(summaries[0]["prediction_requested"])

    def test_separate_optional_sql_remains_permitted_with_forced_prediction(self) -> None:
        calls = (
            RUN.ToolCall(
                "mcp__gsf__ask_question",
                {"question": "current incidents"},
                {
                    "answer": "two",
                    "sql": "SELECT * FROM incidents",
                    "rows": [{"count": 2}],
                },
            ),
            RUN.ToolCall(
                "mcp__gsf__ask_question",
                {"question": "forecast risk", "prediction": True},
                {
                    "answer": "ranked",
                    "sql": "PREDICT risk FOR incidents",
                    "rows": [{"true_prob": 0.7}],
                },
            ),
        )

        observed, failures, summaries = RUN.observed_capabilities(calls)
        attempted = {summary["attempted_capability"] for summary in summaries}
        failures.extend(
            RUN._capability_contract_failures(
                {RUN.GSF_KUMO}, {RUN.GSF_STRUCTURED}, attempted, observed
            )
        )

        self.assertEqual([], failures)
        self.assertEqual({RUN.GSF_STRUCTURED, RUN.GSF_KUMO}, observed)

    def test_prediction_accepts_probability_and_regression_outputs(self) -> None:
        for row in (
            {"entity": "incident-1", "true_prob": 0.7},
            {"entity": "incident-1", "late_true": 0.7},
            {"entity": "incident-1", "risk_prob": 0.7},
            {"entity": "incident-1", "target_pred": 4.2},
            {"entity": "incident-1", "failure_count_pred": 4.2},
            {"ENTITY": "incident-1", "PREDICTION": 4.2},
        ):
            with self.subTest(row=row):
                self.assertTrue(RUN._prediction_rows_are_scored([row]))

    def test_zero_row_structured_and_retriever_results_remain_observed(self) -> None:
        calls = (
            RUN.ToolCall(
                "mcp__gsf__ask_question",
                {"question": "missing incident"},
                {
                    "answer": "no matches",
                    "sql": "SELECT * FROM incidents WHERE id = 'absent'",
                    "rows": [],
                    "row_count": 0,
                },
            ),
            RUN.ToolCall(
                "mcp__retriever__query",
                {
                    "query": "missing policy",
                    "top_k": 5,
                    "format": "hits",
                    "rerank": True,
                    "payload": {"collection_name": "cloud-docs"},
                },
                {"hits": [], "matched_count": 0, "exhaustive": True},
            ),
        )

        observed, failures, summaries = RUN.observed_capabilities(calls, "cloud-docs")

        self.assertEqual({RUN.GSF_STRUCTURED, RUN.NEMO_RETRIEVER}, observed)
        self.assertEqual([], failures)
        self.assertTrue(all(item["observed_capability"] for item in summaries))

    def test_durable_tool_evidence_discards_rows_and_generated_queries(self) -> None:
        durable = RUN._durable_tool_evidence(
            [
                {
                    "tool": "mcp__gsf__ask_question",
                    "capability": RUN.GSF_KUMO,
                    "attempted_capability": RUN.GSF_KUMO,
                    "observed_capability": RUN.GSF_KUMO,
                    "query_language": "pql",
                    "database_name": "supply_chain",
                    "prediction_requested": True,
                    "row_count": 1,
                    "truncated": False,
                    "sql": "PREDICT late_receipt",
                    "rows": [{"purchase_order_id": "PO-1", "true_prob": 0.9}],
                    "matched_count": "private value",
                    "exhaustive": {"private": "value"},
                }
            ]
        )

        self.assertEqual(1, durable[0]["row_count"])
        self.assertTrue(durable[0]["prediction_requested"])
        serialized = json.dumps(durable)
        self.assertNotIn("PREDICT", serialized)
        self.assertNotIn("PO-1", serialized)
        self.assertNotIn("true_prob", serialized)
        self.assertNotIn("private", serialized)

    def test_retriever_contract_requires_explicit_reranking_and_active_collection(
        self,
    ) -> None:
        calls = (
            RUN.ToolCall(
                "mcp__retriever__query",
                {"query": "q", "payload": {"collection_name": "wrong"}},
                {"results": [{"hits": []}]},
            ),
        )

        observed, failures, _ = RUN.observed_capabilities(calls, "expected")

        self.assertEqual({RUN.NEMO_RETRIEVER}, observed)
        self.assertEqual(
            [
                "Retriever query did not request reranked hits",
                "Retriever top_k was not 5",
                "Retriever collection differs: expected 'expected', observed 'wrong'",
            ],
            failures,
        )

    def test_failed_retriever_result_is_attempted_but_not_observed(self) -> None:
        arguments = {
            "query": "q",
            "top_k": 5,
            "format": "hits",
            "rerank": True,
            "payload": {"collection_name": "documents"},
        }
        for output in (
            {"error": "unavailable"},
            {"answer": "backend unavailable"},
            {"hits": "not-a-list"},
            {"hits": ["not-an-object"]},
        ):
            with self.subTest(output=output):
                observed, failures, summaries = RUN.observed_capabilities(
                    [RUN.ToolCall("mcp__retriever__query", arguments, output)],
                    "documents",
                )

                self.assertEqual(set(), observed)
                self.assertEqual(
                    ["mcp__retriever__query returned no valid retrieval result"],
                    failures,
                )
                self.assertEqual(RUN.NEMO_RETRIEVER, summaries[0]["attempted_capability"])
                self.assertIsNone(summaries[0]["observed_capability"])

    def test_result_and_corpus_contracts_use_aiq_fingerprints(self) -> None:
        value = "service-a"
        contract = RUN.CaseContract(
            "case",
            (),
            "hybrid",
            "core",
            (RUN.GSF_STRUCTURED, RUN.NEMO_RETRIEVER),
            {
                "result_value_contracts": [
                    {"id": "service", "values": [{"sha256": RUN._value_sha256(value)}]}
                ],
                "corpus_references": {"recovery-playbook": ["unused anchor"]},
            },
        )
        summaries = [
            {"capability": RUN.GSF_STRUCTURED, "rows": [{"service": value}]},
            {
                "capability": RUN.NEMO_RETRIEVER,
                "rows": [],
                "document_refs": ["recovery-playbook"],
            },
        ]

        checks = RUN._contract_checks(
            contract,
            summaries,
            active_database=None,
            prediction_contract_exists=False,
        )

        self.assertEqual(
            {"result_value_contracts": "pass", "corpus_references": "pass"},
            {check["name"]: check["status"] for check in checks},
        )

    def test_result_contract_uses_all_official_gsf_rows_and_respects_truncation(
        self,
    ) -> None:
        expected = RUN._value_sha256("target")
        contract = RUN.CaseContract(
            "case",
            (),
            "structured_analytic",
            "core",
            (RUN.GSF_STRUCTURED,),
            {
                "result_value_contracts": [
                    {
                        "id": "target",
                        "database_name": "db",
                        "values": [{"sha256": expected}],
                    }
                ]
            },
        )
        rows = [{"value": f"other-{index}"} for index in range(99)] + [
            {"value": "target"}
        ]
        summaries = [
            {
                "capability": RUN.GSF_STRUCTURED,
                "database_name": "db",
                "rows": rows,
                "row_count": 100,
                "truncated": False,
            }
        ]

        checks = RUN._contract_checks(
            contract,
            summaries,
            active_database="db",
            prediction_contract_exists=False,
        )
        self.assertEqual("pass", checks[0]["status"])

        summaries[0]["rows"] = rows[:-1]
        summaries[0]["truncated"] = True
        checks = RUN._contract_checks(
            contract,
            summaries,
            active_database="db",
            prediction_contract_exists=False,
        )
        self.assertEqual("not_observable", checks[0]["status"])

    def test_zero_row_and_retriever_no_match_contracts(self) -> None:
        contract = RUN.CaseContract(
            "case",
            (),
            "structured_analytic",
            "core",
            (RUN.GSF_STRUCTURED, RUN.NEMO_RETRIEVER),
            {
                "sql_result_contract": {"database_name": "db"},
                "retriever_no_match_contract": {
                    "match_type": "exact",
                    "reference": "absent-id",
                },
            },
        )
        summaries = [
            {
                "capability": RUN.GSF_STRUCTURED,
                "database_name": "db",
                "rows": [],
                "row_count": 0,
                "truncated": False,
            },
            {
                "capability": RUN.NEMO_RETRIEVER,
                "document_refs": [],
                "matched_count": None,
                "exhaustive": None,
            },
        ]

        checks = RUN._contract_checks(
            contract,
            summaries,
            active_database="db",
            prediction_contract_exists=False,
        )
        states = {check["name"]: check["status"] for check in checks}
        self.assertEqual("pass", states["sql_result_contract"])
        self.assertEqual("not_observable", states["retriever_no_match_contract"])

    def test_prediction_result_contract_checks_population_and_ranking(self) -> None:
        evidence = {
            "prediction_result_contract": {
                "database_name": "db",
                "population_cardinality": 2,
                "entity_fields": ["entity", "id"],
                "score_fields": ["score", "probability"],
                "ranked": True,
            }
        }
        contract = RUN.CaseContract(
            "case",
            (),
            "structured_predictive",
            "core",
            (RUN.GSF_KUMO,),
            evidence,
            ("a", "b"),
        )
        summaries = [
            {
                "capability": RUN.GSF_KUMO,
                "database_name": "db",
                "rows": [
                    {"entity": "a", "score": 0.9},
                    {"entity": "b", "score": 0.2},
                ],
                "row_count": 2,
                "truncated": False,
            }
        ]

        checks = RUN._contract_checks(
            contract,
            summaries,
            active_database="db",
            prediction_contract_exists=True,
        )
        states = {check["name"]: check["status"] for check in checks}
        self.assertEqual("pass", states["prediction_result_contract"])
        self.assertEqual("not_observable", states["prediction_graph_lineage"])

    def test_suite_contract_includes_report_grouping_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.json"
            path.write_text(
                json.dumps(
                    {
                        "suites": [
                            {
                                "suite": "suite.json",
                                "industry_id": "cloud-services",
                                "industry_name": "Cloud Services",
                                "dataset_id": "cloud-operations",
                                "dataset_name": "Cloud Operations",
                                "case_contracts": [
                                    {
                                        "id": "case",
                                        "source_ids": [],
                                        "profile": "source_help",
                                        "cohort": "core",
                                        "expected_capabilities": [],
                                        "optional_capabilities": [],
                                        "evidence": {},
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            suite = RUN.load_suite_contract(path, "suite.json")

        self.assertEqual("cloud-services", suite.industry_id)
        self.assertEqual("cloud-operations", suite.dataset_id)
        self.assertIn("case", suite.cases)
        self.assertEqual((), suite.cases["case"].optional_capabilities)


class JudgeTests(unittest.TestCase):
    def test_uses_exact_aiq3_v3_rubric(self) -> None:
        self.assertEqual("enterprise_response_usefulness.v3", JUDGE.RUBRIC_ID)
        self.assertEqual(
            "09af73cf74293611992cbbbcbb8f463e26a0e273fc52b263c43b2f71882b0d5b",
            JUDGE.RUBRIC_SHA256,
        )

    def test_optional_configuration_and_empty_answer_are_nonblocking(self) -> None:
        settings, reason = JUDGE.configuration(url="", api_key="", model="", timeout=60)
        self.assertIsNone(settings)
        self.assertEqual("judge_not_configured", reason)
        record = _judged_record("one", status="fail", verdict="unusable")
        record.pop("judge_provenance")
        record.pop("semantic_judge")
        record["answer"] = ""

        result = JUDGE.evaluate_record(record, None, reason)

        self.assertEqual("not_run", result["semantic_judge"]["status"])
        self.assertFalse(result["semantic_judge"]["blocking"])

    def test_responses_request_uses_v3_prompt_and_selected_sources(self) -> None:
        settings = JUDGE.JudgeConfiguration(
            "https://judge.example/v1/responses", "secret", "judge-model", 60
        )
        judge = JUDGE.ResponsesJudge(settings)
        seen: dict = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def read(_limit):
                judgment = {
                    "verdict": "usable",
                    "confidence": 0.9,
                    "reason_codes": ["direct_answer"],
                }
                return json.dumps({"output_text": json.dumps(judgment)}).encode()

        class Opener:
            @staticmethod
            def open(request, timeout):
                seen["payload"] = json.loads(request.data)
                seen["timeout"] = timeout
                return Reply()

        judge.opener = Opener()
        record = _judged_record("one", status="pass", verdict="usable")

        semantic, truncated = judge.score(record)

        self.assertEqual("usable", semantic["verdict"])
        self.assertFalse(truncated)
        self.assertEqual(JUDGE.SYSTEM_PROMPT, seen["payload"]["input"][0]["content"])
        judge_input = json.loads(seen["payload"]["input"][1]["content"])
        self.assertEqual(["cloud_structured"], judge_input["selected_sources"])

    def test_truncated_judge_input_is_not_reported_as_fully_observed(self) -> None:
        record = _judged_record("one", status="pass", verdict="usable")
        record.pop("judge_provenance")
        record.pop("semantic_judge")

        class FakeJudge:
            settings = SimpleNamespace(model="judge-model")

            @staticmethod
            def score(_record):
                return (
                    {
                        "status": "evaluated",
                        "blocking": False,
                        "verdict": "usable",
                        "confidence_milli": 950,
                        "reason_codes": ["direct_answer"],
                    },
                    True,
                )

        result = JUDGE.evaluate_record(record, FakeJudge(), None)

        semantic = result["semantic_judge"]
        self.assertEqual("not_observable", semantic["status"])
        self.assertEqual("usable", semantic["partial_evaluation"]["verdict"])
        self.assertTrue(semantic["input_truncated"])

    def test_judgment_rejects_extra_fields_and_accepts_fenced_json(self) -> None:
        value = JUDGE._judgment(
            '```json\n{"verdict":"degraded","confidence":0.75,'
            '"reason_codes":["partial_answer"]}\n```'
        )
        self.assertEqual("degraded", value["verdict"])
        with self.assertRaisesRegex(JUDGE.JudgeError, "invalid shape"):
            JUDGE._judgment(
                '{"verdict":"usable","confidence":1,"reason_codes":["direct_answer"],"extra":1}'
            )


class ReportTests(unittest.TestCase):
    @staticmethod
    def write_index(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": {
                        "portfolio": "industries/portfolio.v1.json",
                        "portfolio_sha256": "a" * 64,
                        "repository_commit": "b" * 40,
                        "selection": "first canonical wording",
                    },
                    "totals": {
                        "industries": 1,
                        "datasets": 1,
                        "cases": 2,
                        "authored_variants": 3,
                    },
                    "suites": [
                        {
                            "industry_id": "cloud-services",
                            "dataset_id": "cloud-operations",
                            "case_contracts": [
                                {
                                    "id": "one",
                                    "expected_capabilities": [RUN.GSF_STRUCTURED],
                                },
                                {
                                    "id": "two",
                                    "expected_capabilities": [RUN.GSF_STRUCTURED],
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_aggregates_every_case_and_renders_question_answer_status_and_verdict(
        self,
    ) -> None:
        records = [
            _judged_record("one", status="pass", verdict="usable"),
            _judged_record("two", status="fail", verdict="degraded"),
        ]

        report = REPORT.build_report(records)
        markdown = REPORT.render_markdown(report)

        self.assertEqual(2, report["overall"]["cases"])
        self.assertEqual(1, report["overall"]["run_status"]["pass"])
        self.assertEqual(1.0, report["overall"]["helpful_of_evaluated"])
        self.assertEqual("judge-model", report["judge_provenance"]["model"])
        cases = report["industries"][0]["datasets"][0]["cases"]
        self.assertEqual(["one", "two"], [case["case_id"] for case in cases])
        self.assertEqual("Question <one>?", cases[0]["question"])
        self.assertEqual("Answer for two\nwith detail.", cases[1]["answer"])
        self.assertEqual([RUN.GSF_STRUCTURED], cases[0]["attempted_capabilities"])
        self.assertIn("Run pass", markdown)
        self.assertIn("usable", markdown)
        self.assertIn("degraded", markdown)
        self.assertIn("Question &lt;one&gt;?", markdown)
        self.assertIn("Answer for two\nwith detail.", markdown)
        self.assertIn("Attempted capabilities", markdown)
        self.assertIn("Judge model: `judge-model`.", markdown)
        self.assertNotIn("**Tool evidence**", markdown)
        self.assertNotIn("&quot;rows&quot;", markdown)
        report["judge_provenance"]["model"] = None
        self.assertIn(
            "Judge model: `unavailable`.", REPORT.render_markdown(report)
        )

    def test_reports_capability_routing_funnel(self) -> None:
        records = [
            _judged_record(str(index), status="pass", verdict="usable")
            for index in range(4)
        ]
        prediction = RUN.GSF_KUMO
        retriever = RUN.NEMO_RETRIEVER
        records[1]["expected_capabilities"] = [prediction]
        records[1]["optional_capabilities"] = [RUN.GSF_STRUCTURED]
        records[1]["attempted_capabilities"] = [prediction, RUN.GSF_STRUCTURED]
        records[1]["observed_capabilities"] = [RUN.GSF_STRUCTURED]
        records[2]["expected_capabilities"] = [retriever]
        records[2]["attempted_capabilities"] = []
        records[2]["observed_capabilities"] = []
        records[3]["expected_capabilities"] = []
        records[3]["attempted_capabilities"] = [RUN.GSF_STRUCTURED]
        records[3]["observed_capabilities"] = [RUN.GSF_STRUCTURED]

        report = REPORT.build_report(records)
        overall = report["routing"]["overall"]
        self.assertEqual({"count": 2, "rate": 0.5}, overall["exact_attempt"])
        self.assertEqual({"count": 1, "rate": 0.25}, overall["exact_observation"])
        self.assertEqual(1, overall["missing_attempt"]["count"])
        self.assertEqual(1, overall["attempted_not_observed"]["count"])
        self.assertEqual(1, overall["unexpected_attempt"]["count"])

        predictive = report["routing"]["by_capability"][prediction]
        self.assertEqual({"count": 1, "rate": 0.25}, predictive["expected"])
        self.assertEqual(
            {"count": 1, "rate": 1.0}, predictive["attempted_when_expected"]
        )
        self.assertEqual(
            {"count": 0, "rate": 0.0}, predictive["attempt_to_observation"]
        )
        structured = report["routing"]["by_capability"][RUN.GSF_STRUCTURED]
        self.assertEqual(
            {"count": 1, "rate": 0.5}, structured["unexpected_attempt"]
        )
        combinations = report["routing"]["by_expected_capabilities"]
        self.assertEqual(4, len(combinations))
        self.assertEqual([], combinations[0]["expected_capabilities"])
        prediction_path = next(
            item for item in combinations if item["expected_capabilities"] == [prediction]
        )
        self.assertEqual(1, prediction_path["attempted_not_observed"]["count"])

        markdown = REPORT.render_markdown(report)
        self.assertNotIn("273 of 281", markdown)
        report["scope"] = {"cases": REPORT.AIQ3_INDUSTRY_BOUND_TASKS}
        self.assertIn("273 of 281", REPORT.render_markdown(report))
        self.assertIn("`PREDICT` is an attempt", markdown)
        self.assertIn("Judge not observable", markdown)
        self.assertNotIn("Judge unavailable/not run", markdown)
        self.assertIn("Allowed optional capabilities", markdown)

    def test_routing_rejects_invalid_capability_sets(self) -> None:
        invalid_values = (
            None,
            RUN.GSF_STRUCTURED,
            ["unknown"],
            [RUN.GSF_STRUCTURED, RUN.GSF_STRUCTURED],
            [[]],
        )
        for value in invalid_values:
            with self.subTest(value=value):
                record = _judged_record("one", status="pass", verdict="usable")
                record["expected_capabilities"] = value
                with self.assertRaisesRegex(
                    REPORT.ReportError, "unique list of known capabilities"
                ):
                    REPORT.build_report([record])

        record = _judged_record("one", status="pass", verdict="usable")
        record["attempted_capabilities"] = []
        with self.assertRaisesRegex(
            REPORT.ReportError, "observed capabilities without attempts"
        ):
            REPORT.build_report([record])

    def test_loader_rejects_duplicates_across_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / "one.jsonl", Path(temporary) / "two.jsonl"]
            line = (
                json.dumps(_judged_record("one", status="pass", verdict="usable"))
                + "\n"
            )
            for path in paths:
                path.write_text(line, encoding="utf-8")

            with self.assertRaisesRegex(REPORT.ReportError, "duplicate case_id"):
                REPORT.load_records(paths)

    def test_loader_rejects_verdict_for_non_evaluated_judgment(self) -> None:
        record = _judged_record("one", status="pass", verdict="usable")
        record["semantic_judge"]["status"] = "unavailable"

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "judged.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                REPORT.ReportError, "verdict without an evaluated judgment"
            ):
                REPORT.load_records([path])

    def test_complete_coverage_is_default_and_partial_is_explicit(self) -> None:
        records = [_judged_record("one", status="pass", verdict="usable")]
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            self.write_index(index)
            with self.assertRaisesRegex(REPORT.ReportError, "incomplete"):
                REPORT.validate_coverage(records, index)

            coverage = REPORT.validate_coverage(records, index, allow_partial=True)
            report = REPORT.build_report(records, coverage)
            self.assertEqual("partial", coverage["status"])
            self.assertEqual("a" * 64, report["source"]["portfolio_sha256"])
            self.assertEqual(3, report["scope"]["authored_variants"])
            markdown = REPORT.render_markdown(report)
            self.assertIn("partial evaluation report", markdown)
            self.assertIn("Missing case IDs: `two`", markdown)
            self.assertIn("repository commit `" + "b" * 40 + "`", markdown)
            self.assertNotIn("273 of 281", markdown)

    def test_coverage_rejects_expected_capability_mismatch(self) -> None:
        records = [
            _judged_record("one", status="pass", verdict="usable"),
            _judged_record("two", status="pass", verdict="usable"),
        ]
        records[0]["expected_capabilities"] = []
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            self.write_index(index)

            with self.assertRaisesRegex(
                REPORT.ReportError,
                "expected capabilities differ from compiled contract",
            ):
                REPORT.validate_coverage(records, index)
            records[0]["expected_capabilities"] = [RUN.GSF_STRUCTURED]
            records[0]["optional_capabilities"] = [RUN.GSF_KUMO]
            with self.assertRaisesRegex(
                REPORT.ReportError,
                "optional capabilities differ from compiled contract",
            ):
                REPORT.validate_coverage(records, index)

if __name__ == "__main__":
    unittest.main()
