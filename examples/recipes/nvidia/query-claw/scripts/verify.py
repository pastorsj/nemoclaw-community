#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify Query Claw's local assets and optional managed MCP endpoints."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from generate_data import DEFAULT_SPEC, generate, validate


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = EXAMPLE_ROOT / "config" / "tool-contracts.json"


def load_contracts(path: Path = DEFAULT_CONTRACTS) -> dict[str, Any]:
    contracts = json.loads(path.read_text(encoding="utf-8"))
    registration = contracts.get("registration")
    routes = contracts.get("routes")
    if (
        contracts.get("schema_version") != 1
        or not isinstance(registration, dict)
        or not isinstance(routes, dict)
        or not routes
    ):
        raise ValueError(
            "tool contracts must use schema version 1 and define registration and routes"
        )
    if not all(registration.get(key) for key in ("name", "token_env")):
        raise ValueError("tool contract registration metadata is incomplete")
    all_tools: list[str] = []
    for route, tools in routes.items():
        if (
            not isinstance(tools, list)
            or not tools
            or any(not isinstance(value, str) or not value for value in tools)
            or len(tools) != len(set(tools))
        ):
            raise ValueError(f"{route} has an invalid tools list")
        all_tools.extend(tools)
    if len(all_tools) != len(set(all_tools)):
        raise ValueError("tool names must be unique across routes")
    return contracts


def check_inventory(
    server: str, expected_tools: list[str], advertised: list[str]
) -> None:
    tools = set(advertised)
    expected = set(expected_tools)
    if expected - tools:
        raise ValueError(
            f"{server} is missing required tools: {sorted(expected - tools)}"
        )
    if tools - expected:
        raise ValueError(
            f"{server} advertises tools outside its allowed contract: {sorted(tools - expected)}"
        )


def discovery_tools(server: str, discovery: dict[str, Any]) -> list[str]:
    tools = discovery.get("tools")
    if discovery.get("ok") is not True or not isinstance(tools, list):
        detail = discovery.get("detail")
        suffix = f": {detail}" if isinstance(detail, str) and detail else ""
        raise ValueError(f"{server} tool discovery did not succeed{suffix}")
    if discovery.get("truncated") is not False:
        raise ValueError(f"{server} tool discovery was truncated")
    if discovery.get("count") != len(tools):
        raise ValueError(f"{server} tool discovery count does not match returned tools")
    return tools


PRIVATE_DISCOVERY_LIMIT = (
    "tool discovery skipped: no valid managed endpoint is available"
)


def managed_bridge_tools(server: str, status: dict[str, Any]) -> list[str] | None:
    support = status.get("support", {})
    provider = status.get("provider", {})
    policy = status.get("policy", {})
    adapter = status.get("adapter", {})
    target = status.get("trustedPrivateTarget", {})
    checks = {
        "Hermes adapter support": status.get("agent") == "hermes"
        and support.get("supported") is True
        and support.get("mode") == "bridge"
        and support.get("adapter") == "hermes-config",
        "credential provider": provider.get("registryPresent") is True
        and provider.get("gatewayPresent") is True
        and provider.get("attached") is True
        and provider.get("credentialReady") is True,
        "generated policy": policy.get("registryPresent") is True
        and policy.get("gatewayPresent") is True,
        "Hermes registration": adapter.get("registered") is True,
        "private DNS pins": target.get("state") == "match"
        and bool(target.get("recordedPins")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(
            f"{server} native private bridge is not ready: {', '.join(failed)}"
        )

    discovery = status.get("toolDiscovery", {})
    if discovery.get("ok") is True:
        return discovery_tools(server, discovery)
    # NemoClaw v0.0.120 cannot run its optional tools/list diagnostic against
    # an explicitly trusted private hostname. Accept only that exact skip after
    # every enforceable native provider, policy, adapter, and DNS-pin check
    # above succeeds. Endpoint implementations remain contract-tested locally;
    # the live evaluator then requires successful routed tool calls.
    if discovery.get("detail") == PRIVATE_DISCOVERY_LIMIT:
        return None
    return discovery_tools(server, discovery)


def validate_skill() -> None:
    skills = EXAMPLE_ROOT / "skills"
    required_files = {
        skills / "query-claw" / "SKILL.md": (
            "name: query-claw",
            "Select sources",
            "Sources used:",
            "configured read-only Query Claw tools",
            "unavailable named source",
        ),
        skills / "query-claw-structured" / "SKILL.md": (
            "name: query-claw-structured",
            "mcp__query_claw__check_readiness",
            "mcp__query_claw__ask_question",
        ),
        skills / "query-claw-documents" / "SKILL.md": (
            "name: query-claw-documents",
            "mcp__query_claw__query",
            "citation-ready evidence",
        ),
        skills / "query-claw-predictive" / "SKILL.md": (
            "name: query-claw-predictive",
            "mcp__query_claw__inspect_graph_metadata",
            "mcp__query_claw__predict",
            "mcp__query_claw__explain",
            "Label each result **Predicted**",
        ),
        skills / "query-claw-reporting" / "SKILL.md": (
            "name: query-claw-reporting",
            "Markdown",
            "text chart",
        ),
        skills / "query-claw" / "references" / "evidence.md": (
            "Observed",
            "Predicted",
            "Calculated",
        ),
    }
    missing = [
        path.relative_to(EXAMPLE_ROOT).as_posix()
        for path in required_files
        if not path.is_file()
    ]
    if missing:
        raise ValueError(f"skill files are missing: {missing}")
    for path, rules in required_files.items():
        body = path.read_text(encoding="utf-8")
        for rule in rules:
            if rule not in body:
                relative = path.relative_to(EXAMPLE_ROOT).as_posix()
                raise ValueError(f"{relative} is missing required guidance: {rule}")


def local_verify(spec: Path) -> dict[str, object]:
    with (
        tempfile.TemporaryDirectory(prefix="query-claw-") as first_dir,
        tempfile.TemporaryDirectory(prefix="query-claw-") as second_dir,
    ):
        first = Path(first_dir)
        second = Path(second_dir)
        first_manifest = generate(spec, first)
        second_manifest = generate(spec, second)
        if first_manifest["fingerprint"] != second_manifest["fingerprint"]:
            raise ValueError("the same seed produced different data fingerprints")
        summary = validate(first)

    contracts = load_contracts()
    validate_skill()

    print(f"PASS deterministic data  {summary['fingerprint'][:16]}")
    print(
        f"PASS service boundary    {summary['service_files']} service files; labels held separately"
    )
    print(
        f"PASS entity integrity    {summary['purchase_orders']} orders; {summary['documents']} supplier notices"
    )
    print(
        f"PASS temporal boundary   {summary['evaluation_orders']} prediction labels withheld after cutoff"
    )
    print(
        f"PASS tool contracts      {len(contracts['routes'])} contract-bounded routes"
    )
    print("PASS routing skills      5 focused skills; one shared evidence contract")
    print("Query Claw local verification: 6/6 checks passed")
    return summary


def live_verify(sandbox: str, cli: str, contracts_path: Path) -> None:
    contracts = load_contracts(contracts_path)
    registration = contracts["registration"]
    expected = [tool for tools in contracts["routes"].values() for tool in tools]
    command = [
        cli,
        sandbox,
        "mcp",
        "status",
        registration["name"],
        "--tools",
        "--json",
    ]
    for attempt in range(12):
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=300
        )
        status = json.loads(result.stdout)
        try:
            tools = managed_bridge_tools(registration["name"], status)
            break
        except ValueError:
            if attempt == 11:
                raise
            time.sleep(5)
    if tools is None:
        print("PASS live query-claw native private bridge ready")
    else:
        check_inventory(registration["name"], expected, tools)
        print(f"PASS live query-claw {', '.join(tools)}")
    print("Query Claw live managed-MCP verification: 1/1 endpoint passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local", action="store_true", help="run credential-free local verification"
    )
    parser.add_argument(
        "--live", action="store_true", help="also inspect registered managed MCP tools"
    )
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument(
        "--sandbox", default=os.environ.get("NEMOCLAW_SANDBOX_NAME", "query-claw")
    )
    parser.add_argument("--cli", default=os.environ.get("NEMOCLAW_CLI", "nemohermes"))
    args = parser.parse_args()
    if not args.local and not args.live:
        parser.error("select --local or --live")
    local_verify(args.spec.resolve())
    if args.live:
        live_verify(args.sandbox, args.cli, args.contracts.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
