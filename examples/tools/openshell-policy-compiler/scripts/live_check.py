#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply a compiled policy to a throwaway OpenShell sandbox and probe it.

The script creates a sandbox (or uses one you name), merges the compiled
network policy into the sandbox's base policy, applies it with
``openshell policy set --wait``, and runs each probe with ``curl`` inside the
sandbox. A probe passes when OpenShell allows or denies it as expected. A
request counts as denied by OpenShell when the response is a 403 whose body
contains ``policy_denied``, or when the connection is refused before an HTTP
response.

Probes send real requests to the hosts the policy allows. Use only probes whose
requests are safe to send, such as unauthenticated reads.

Usage:
  scripts/live_check.py --policy out/github-repo-explainer/policy.yaml \\
      --probes examples/github-repo-explainer/probes.yaml [--sandbox NAME] [--keep]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml


def run(args: list[str], *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
    return result


def base_policy(sandbox: str) -> dict:
    text = run(["openshell", "policy", "get", sandbox, "--base"]).stdout
    _, _, body = text.partition("\n---\n")
    return yaml.safe_load(body or text)


def probe(sandbox: str, item: dict) -> dict:
    method = item.get("method", "GET")
    args = [
        "openshell", "sandbox", "exec", "-n", sandbox, "--no-tty", "--timeout", "60", "--",
        "curl", "-sS", "-m", "30", "-X", method, "-w", "\n%{http_code}",
    ]
    if item.get("body") is not None:
        args += ["-H", "Content-Type: application/json", "--data", json.dumps(item["body"])]
    args.append(item["url"])
    result = run(args, timeout=120, check=False)
    output = result.stdout
    body, _, code = output.rstrip("\n").rpartition("\n")
    code = code.strip() or "000"
    denied = (code == "403" and "policy_denied" in body) or code == "000"
    outcome = "deny" if denied else "allow"
    return {
        "method": method,
        "url": item["url"],
        "expect": item["expect"],
        "outcome": outcome,
        "status": code,
        "passed": outcome == item["expect"],
        "why": item.get("why", ""),
        "detail": (body.strip().replace("\n", " ")[:160] if denied else ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", required=True, help="Compiled policy.yaml.")
    parser.add_argument("--probes", required=True, help="YAML list of {method, url, expect: allow|deny, why}.")
    parser.add_argument("--sandbox", help="Existing sandbox to use. Default: create opc-live-<time> and delete it.")
    parser.add_argument("--keep", action="store_true", help="Keep a sandbox this script created.")
    parser.add_argument("--results", help="Write results as JSON to this file.")
    args = parser.parse_args()

    compiled = yaml.safe_load(Path(args.policy).read_text())
    probes = yaml.safe_load(Path(args.probes).read_text())["probes"]
    created = None
    sandbox = args.sandbox
    if not sandbox:
        sandbox = created = f"opc-live-{int(time.time())}"
        print(f"creating sandbox {sandbox} (first use pulls the base image)...", flush=True)
        run(["openshell", "sandbox", "create", "--name", sandbox, "--no-auto-providers", "--no-tty", "--", "true"], timeout=900)
    try:
        merged = base_policy(sandbox)
        merged["network_policies"] = compiled.get("network_policies", {})
        merged_path = Path(args.policy).with_name("applied-policy.yaml")
        merged_path.write_text(yaml.safe_dump(merged, sort_keys=False))
        print(f"applying {args.policy} to {sandbox}...", flush=True)
        applied = run(["openshell", "policy", "set", sandbox, "--policy", str(merged_path), "--wait"], timeout=300)
        print(applied.stdout.strip())
        results = [probe(sandbox, item) for item in probes]
    finally:
        if created and not args.keep:
            run(["openshell", "sandbox", "delete", created], check=False)
            print(f"deleted sandbox {created}")

    print()
    print("| Result | Expect | Outcome | Status | Request | Why |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"| {mark} | {r['expect']} | {r['outcome']} | {r['status']} | {r['method']} {r['url']} | {r['why']} |")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} probes behaved as expected.")
    if args.results:
        Path(args.results).write_text(json.dumps(results, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
