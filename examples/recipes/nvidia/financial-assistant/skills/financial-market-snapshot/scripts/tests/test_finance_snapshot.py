# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

finance_snapshot = importlib.import_module("finance_snapshot")


class FinanceSnapshotTest(unittest.TestCase):
    def test_normalize_symbol_cleans_common_ticker_input(self) -> None:
        self.assertEqual(finance_snapshot.normalize_symbol("  brk/b  "), "BRK-B")
        with self.assertRaisesRegex(ValueError, "empty ticker"):
            finance_snapshot.normalize_symbol("   ")
        for invalid in (
            "../NVDA",
            "NVDA?range=1y",
            "NVDA#fragment",
            "NV DA",
            "NVDA%2FMSFT",
            "NVDA-",
            "NVDÁ",
            "A" * (finance_snapshot.MAX_SYMBOL_LENGTH + 1),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "invalid ticker symbol"),
            ):
                finance_snapshot.normalize_symbol(invalid)

    def test_fetch_quotes_rejects_empty_and_oversized_requests(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one ticker"):
            finance_snapshot.fetch_quotes([], timeout=1)
        with self.assertRaisesRegex(ValueError, "at most 20 tickers"):
            finance_snapshot.fetch_quotes(
                [f"TEST{index}" for index in range(finance_snapshot.MAX_TICKERS + 1)],
                timeout=1,
            )

    def test_fetch_quote_parses_latest_values_and_change_math(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {
                            "symbol": "^GSPC",
                            "exchangeName": "NMS",
                            "currency": "USD",
                            "chartPreviousClose": 100.0,
                        },
                        "timestamp": [1_700_000_000, None, 1_700_086_400],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [99.0, None, 101.0],
                                    "high": [103.0, None, 104.0],
                                    "low": [98.0, None, 100.0],
                                    "close": [102.0, None, 102.5],
                                    "volume": [10, None, 25],
                                }
                            ]
                        },
                    }
                ],
            }
        }
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))

        with mock.patch.object(
            finance_snapshot.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            quote = finance_snapshot.fetch_quote(" ^gspc ", timeout=7)

        request = urlopen.call_args.args[0]
        self.assertIn("/%5EGSPC?range=5d&interval=1d", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)
        self.assertEqual(quote.symbol, "^GSPC")
        self.assertEqual(quote.timestamp, 1_700_086_400)
        self.assertEqual(quote.open, 101.0)
        self.assertEqual(quote.close, 102.5)
        self.assertEqual(quote.volume, 25)
        self.assertEqual(quote.change, 2.5)
        self.assertEqual(quote.change_percent, 2.5)

    def test_change_percent_is_undefined_for_zero_previous_close(self) -> None:
        quote = finance_snapshot.Quote(
            symbol="TEST",
            exchange_name=None,
            currency=None,
            timestamp=None,
            open=None,
            high=None,
            low=None,
            close=4.0,
            previous_close=0.0,
            volume=None,
        )
        self.assertEqual(quote.change, 4.0)
        self.assertIsNone(quote.change_percent)


if __name__ == "__main__":
    unittest.main()
