# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

sec_company_facts = importlib.import_module("sec_company_facts")


class SecCompanyFactsTest(unittest.TestCase):
    def tearDown(self) -> None:
        sec_company_facts.ticker_index.cache_clear()

    def test_resolve_ticker_normalizes_and_validates_input(self) -> None:
        company = {"ticker": "NVDA", "cik": "0001045810", "title": "NVIDIA CORP"}
        with mock.patch.object(
            sec_company_facts, "ticker_index", return_value={"NVDA": company}
        ):
            self.assertEqual(sec_company_facts.resolve_ticker(" nvda ", 3), company)
            with self.assertRaisesRegex(ValueError, "ticker not found"):
                sec_company_facts.resolve_ticker("missing", 3)
            with self.assertRaisesRegex(ValueError, "empty ticker"):
                sec_company_facts.resolve_ticker(" ", 3)
            for invalid in ("../NVDA", "NVDA?", "NV DA", "NVDÁ", "NVDA-"):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(ValueError, "invalid SEC ticker"),
                ):
                    sec_company_facts.resolve_ticker(invalid, 3)

    def test_user_agent_requires_identity_and_contact_email(self) -> None:
        valid = "Example Research Team sec-contact@example.org"
        with mock.patch.dict(
            sec_company_facts.os.environ, {"SEC_USER_AGENT": valid}, clear=True
        ):
            self.assertEqual(sec_company_facts.user_agent(), valid)

        for invalid in (
            "",
            "sec-contact@example.org",
            "Example Research Team",
            "Example Research Team sec-contact@example.org\r\nInjected: yes",
        ):
            with (
                self.subTest(invalid=invalid),
                mock.patch.dict(
                    sec_company_facts.os.environ,
                    {"SEC_USER_AGENT": invalid},
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "identify the caller"),
            ):
                sec_company_facts.user_agent()

    def test_ticker_index_skips_malformed_upstream_placeholders(self) -> None:
        payload = {
            "0": {"ticker": "NONE.", "cik_str": 1, "title": "Placeholder"},
            "1": {"ticker": "NVDA", "cik_str": 1045810, "title": "NVIDIA CORP"},
        }
        with mock.patch.object(sec_company_facts, "fetch_json", return_value=payload):
            index = sec_company_facts.ticker_index(timeout=3)
        self.assertEqual(set(index), {"NVDA"})
        self.assertEqual(index["NVDA"]["cik"], "0001045810")

    def test_latest_fact_selects_latest_filing_and_ignores_invalid_forms(self) -> None:
        metric = {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "val": 80,
                        "filed": "2025-02-20",
                        "end": "2024-12-31",
                        "fy": 2024,
                    },
                    {
                        "form": "10-Q",
                        "val": 100,
                        "filed": "2025-05-20",
                        "end": "2025-03-31",
                        "fy": 2025,
                    },
                    {
                        "form": "8-K",
                        "val": 999,
                        "filed": "2026-01-01",
                        "end": "2025-12-31",
                    },
                    {"form": "10-Q", "filed": "2026-02-01"},
                ]
            }
        }

        self.assertEqual(
            sec_company_facts.latest_fact(metric),
            {
                "form": "10-Q",
                "val": 100,
                "filed": "2025-05-20",
                "end": "2025-03-31",
                "fy": 2025,
                "unit": "USD",
            },
        )
        self.assertIsNone(
            sec_company_facts.latest_fact(
                {"units": {"USD": [{"form": "8-K", "val": 1}]}}
            )
        )

    def test_summarize_facts_uses_mocked_company_and_company_facts(self) -> None:
        company = {"ticker": "NVDA", "cik": "0001045810", "title": "NVIDIA CORP"}
        facts = {
            "entityName": "NVIDIA Corporation",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "form": "10-K",
                                    "val": 60_000,
                                    "filed": "2025-02-26",
                                    "end": "2025-01-26",
                                    "fy": 2025,
                                }
                            ]
                        }
                    },
                    "NetIncomeLoss": {"units": {"USD": [{"form": "8-K", "val": 1}]}},
                    "NotASelectedMetric": {
                        "units": {"USD": [{"form": "10-K", "val": 2}]}
                    },
                }
            },
        }

        with (
            mock.patch.object(
                sec_company_facts, "resolve_ticker", return_value=company
            ) as resolve_ticker,
            mock.patch.object(
                sec_company_facts, "fetch_json", return_value=facts
            ) as fetch_json,
        ):
            summary = sec_company_facts.summarize_facts("nvda", timeout=9)

        resolve_ticker.assert_called_once_with("nvda", 9)
        fetch_json.assert_called_once_with(
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json",
            timeout=9,
        )
        self.assertEqual(
            summary["company"],
            {
                "ticker": "NVDA",
                "cik": "0001045810",
                "title": "NVIDIA CORP",
                "entity_name": "NVIDIA Corporation",
            },
        )
        self.assertEqual(set(summary["metrics"]), {"Revenues"})
        self.assertEqual(summary["metrics"]["Revenues"]["val"], 60_000)
        self.assertEqual(summary["metrics"]["Revenues"]["unit"], "USD")


if __name__ == "__main__":
    unittest.main()
