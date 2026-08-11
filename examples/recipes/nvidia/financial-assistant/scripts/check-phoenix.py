#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Require a correlated successful LLM + terminal-tool trace in Phoenix."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone


def get_json(url: str, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6006")
    parser.add_argument("--project", default="financial-assistant")
    parser.add_argument("--start-time", required=True)
    parser.add_argument("--wait-seconds", type=int, default=45)
    args = parser.parse_args()

    # Reject a malformed timestamp before beginning the poll.
    datetime.fromisoformat(args.start_time.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    project = urllib.parse.quote(args.project, safe="")
    query = urllib.parse.urlencode({"limit": 1000, "start_time": args.start_time})
    url = f"{args.base_url.rstrip('/')}/v1/projects/{project}/spans?{query}"
    deadline = time.monotonic() + args.wait_seconds
    last_spans: list[dict[str, object]] = []

    while time.monotonic() < deadline:
        try:
            payload = get_json(url, timeout=10)
            data = payload.get("data", [])
            last_spans = data if isinstance(data, list) else []
        except (OSError, ValueError):
            last_spans = []

        by_trace: dict[str, list[dict[str, object]]] = defaultdict(list)
        for span in last_spans:
            if not isinstance(span, dict):
                continue
            context = span.get("context")
            if not isinstance(context, dict):
                continue
            trace_id = str(context.get("trace_id") or "")
            if trace_id:
                by_trace[trace_id].append(span)

        for trace_id, spans in by_trace.items():
            kinds = {str(span.get("span_kind", "")).upper() for span in spans}
            llm_spans = [
                span
                for span in spans
                if str(span.get("span_kind", "")).upper() == "LLM"
            ]
            successful_tools = [
                span
                for span in spans
                if str(span.get("span_kind", "")).upper() == "TOOL"
                and str(span.get("status_code", "")).upper() == "OK"
            ]
            terminal_tools = [
                span
                for span in successful_tools
                if "terminal" in str(span.get("name", "")).lower()
                or "terminal"
                in json.dumps(span.get("attributes", {}), sort_keys=True).lower()
            ]
            llm_statuses = {
                str(span.get("status_code", "")).upper() for span in llm_spans
            }
            if llm_spans and llm_statuses == {"OK"} and terminal_tools:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "trace_id": trace_id,
                            "span_count": len(spans),
                            "span_kinds": sorted(kinds),
                            "llm_statuses": sorted(llm_statuses),
                            "terminal_status": terminal_tools[0].get("status_code"),
                            "terminal_span": terminal_tools[0].get("name"),
                        },
                        indent=2,
                    )
                )
                return 0
        time.sleep(2)

    summary = [
        {
            "name": span.get("name"),
            "kind": span.get("span_kind"),
            "status": span.get("status_code"),
        }
        for span in last_spans[-20:]
        if isinstance(span, dict)
    ]
    raise RuntimeError(
        "Phoenix did not receive a correlated trace with only OK LLM spans and "
        "an OK terminal-tool span "
        f"after {args.start_time}; recent spans={summary}"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise SystemExit(1) from None
