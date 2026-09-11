#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify Query Claw's local assets and optional managed MCP endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from generate_data import DEFAULT_SPEC, generate, validate


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = EXAMPLE_ROOT / "tools" / "tool-contracts.json"
OFFICIAL_RETRIEVER_SKILL_SHA256 = (
    "83109842650c2361218b4387ff379f1b722beff03e401cd914b0b994c6ee0660"
)
NATIVE_RETRIEVER_TOOLS = [
    "answer",
    "get_document",
    "get_job",
    "health",
    "list_job_documents",
    "pipeline_config",
    "query",
]
RETRIEVER_DENY_TOOLS = [
    tool for tool in NATIVE_RETRIEVER_TOOLS if tool != "query"
]


def nemoclaw_registry_path() -> Path:
    port = os.environ.get("NEMOCLAW_GATEWAY_PORT", "8080")
    root = Path.home() / ".nemoclaw"
    if port == "8080":
        return root / "sandboxes.json"
    return root / "gateways" / port / "sandboxes.json"


def check_retriever_deny_intent(
    sandbox: str, status: dict[str, Any], registry_path: Path
) -> None:
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        bridge = registry["sandboxes"][sandbox]["mcp"]["bridges"]["retriever"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Retriever durable denied-tool intent is unavailable") from exc
    if not isinstance(bridge, dict) or bridge.get("denyTools", []) != RETRIEVER_DENY_TOOLS:
        raise ValueError("Retriever durable denied tools differ from its contract")
    if "pendingDenyTools" in bridge:
        raise ValueError("Retriever has an incomplete denied-tool update")
    if "denyTools" in status and status["denyTools"] != RETRIEVER_DENY_TOOLS:
        raise ValueError("Retriever reported denied tools differ from its contract")


def load_contracts(path: Path = DEFAULT_CONTRACTS) -> dict[str, Any]:
    contracts = json.loads(path.read_text(encoding="utf-8"))
    registrations = contracts.get("registrations")
    routes = contracts.get("routes")
    if (
        contracts.get("schema_version") != 2
        or not isinstance(registrations, dict)
        or not registrations
        or not isinstance(routes, dict)
        or not routes
    ):
        raise ValueError(
            "tool contracts must use schema version 2 and define registrations and routes"
        )
    for name, registration in registrations.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(registration, dict)
            or registration.get("auth") not in {"oauth", "managed-bearer"}
            or (
                registration.get("auth") == "managed-bearer"
                and not registration.get("token_env")
            )
        ):
            raise ValueError("tool contract registration metadata is incomplete")
    full_tools: set[tuple[str, str]] = set()
    for route, definition in routes.items():
        if not isinstance(definition, dict) or set(definition) != {
            "registration",
            "tools",
        }:
            raise ValueError(f"{route} has an invalid route definition")
        registration = definition["registration"]
        tools = definition["tools"]
        if (
            registration not in registrations
            or not isinstance(tools, list)
            or not tools
            or any(not isinstance(value, str) or not value for value in tools)
            or len(tools) != len(set(tools))
        ):
            raise ValueError(f"{route} has an invalid tools list")
        qualified = {(registration, tool) for tool in tools}
        if full_tools & qualified:
            raise ValueError("qualified tool names must be unique across routes")
        full_tools.update(qualified)
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
    # NemoClaw v0.0.123 cannot run its optional tools/list diagnostic against
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
            "Select a dataset and sources",
            "Sources used:",
            "configured Query Claw data tools",
            "requested dataset is unavailable",
        ),
        skills / "query-claw-structured" / "SKILL.md": (
            "name: query-claw-structured",
            "mcp__gsf__ask_question",
        ),
        skills / "retriever-mcp" / "SKILL.md": (
            "name: retriever-mcp",
            "Start with `top_k=5` and `format=\"hits\"`",
            "ground your answer",
        ),
        skills / "query-claw-predictive" / "SKILL.md": (
            "name: query-claw-predictive",
            "mcp__gsf__ask_question",
            "Label each result **Predicted**",
        ),
        skills / "query-claw" / "references" / "evidence.md": (
            "Observed",
            "Predicted",
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
    retriever_skill = (skills / "retriever-mcp" / "SKILL.md").read_bytes()
    if hashlib.sha256(retriever_skill).hexdigest() != OFFICIAL_RETRIEVER_SKILL_SHA256:
        raise ValueError("skills/retriever-mcp/SKILL.md differs from its pinned upstream source")


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
    print("PASS routing skills      4 focused skills; one shared evidence contract")
    print("Query Claw local verification: 6/6 checks passed")
    return summary


def live_verify(
    sandbox: str,
    cli: str,
    contracts_path: Path,
    registry_path: Path | None = None,
) -> None:
    contracts = load_contracts(contracts_path)
    retriever_enabled = os.environ.get("QUERY_CLAW_ENABLE_RETRIEVER", "1")
    if retriever_enabled not in {"0", "1"}:
        raise ValueError("QUERY_CLAW_ENABLE_RETRIEVER must be 0 or 1")
    if retriever_enabled == "0":
        print("PASS live Retriever intentionally absent for the active dataset")
        print("Query Claw live managed-MCP verification: selected surface passed")
        return
    registration_name = "retriever"
    expected = contracts["routes"]["retriever"]["tools"]
    command = [
        cli,
        sandbox,
        "mcp",
        "status",
        registration_name,
        "--tools",
        "--json",
    ]
    for attempt in range(12):
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=300
            )
            status = json.loads(result.stdout)
            if not isinstance(status, dict):
                raise ValueError("status command returned a non-object JSON payload")
            tools = managed_bridge_tools(registration_name, status)
            break
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            if attempt < 11:
                time.sleep(5)
                continue
            raise RuntimeError(
                f"{registration_name} MCP status did not become ready after 12 attempts"
            ) from exc
    check_retriever_deny_intent(
        sandbox, status, registry_path or nemoclaw_registry_path()
    )
    if tools is None:
        print("PASS live retriever native private bridge ready")
    else:
        check_inventory(registration_name, NATIVE_RETRIEVER_TOOLS, tools)
        if not set(expected) <= set(tools):
            raise ValueError("native Retriever does not advertise the model-visible query tool")
        print(f"PASS live {registration_name} native endpoint: {', '.join(tools)}")
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
