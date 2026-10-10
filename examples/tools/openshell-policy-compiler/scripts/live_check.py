#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply a compiled policy to a throwaway OpenShell sandbox and probe it.

The script creates a sandbox (or uses one you name), merges the compiled
network policy into the sandbox's base policy, applies it with
``openshell policy set --wait``, and runs each probe with ``curl`` inside the
sandbox. A probe passes when OpenShell allows or denies it as expected. A
request counts as denied by OpenShell when the response is a 403 from the
OpenShell proxy (a ``policy_denied`` body, or an ``X-OpenShell-Policy`` header
for HEAD), or when curl reports that the proxy refused the tunnel
(``CONNECT tunnel failed, response 403``). Anything else that produces no
response, such as a failed ``sandbox exec``, a DNS error, or a timeout, is an
error and fails the run: it proves neither outcome.

Probes send real requests to the hosts the policy allows. Use only probes whose
requests are safe to send, such as unauthenticated reads.

With ``--sandbox NAME`` the script replaces that sandbox's network policy while
the probes run. Before it changes anything it saves the sandbox's current policy
(``openshell policy get NAME --base``) to a temporary file, and it aborts if it
cannot. Afterwards, even when the apply or a probe fails, it restores that file
with ``openshell policy set NAME --policy FILE --wait``. If the restore fails, it
prints the saved file's path and exits non-zero. It never touches another
sandbox or the global policy.

Usage:
  scripts/live_check.py --policy out/github-repo-explainer/policy.yaml \\
      --probes examples/github-repo-explainer/probes.yaml [--sandbox NAME] [--keep]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

# The same header handling that lets ``load_policy_file`` read saved ``policy get`` output.
from policy_compiler.backends.openshell import _strip_policy_get_header


def run(args: list[str], *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
    return result


def base_policy(sandbox: str) -> tuple[str, dict]:
    """Return the sandbox's base policy YAML exactly as OpenShell printed it, and parsed."""
    text = run(["openshell", "policy", "get", sandbox, "--base"]).stdout
    body = _strip_policy_get_header(text)
    try:
        policy = yaml.safe_load(body) if body != text else None
    except yaml.YAMLError as error:
        raise SystemExit(f"could not parse the policy of sandbox {sandbox}: {error}") from None
    if not isinstance(policy, dict):
        raise SystemExit(
            f"could not read the policy of sandbox {sandbox}: expected `Key: value` lines, `---`, "
            f"then a policy mapping from `openshell policy get {sandbox} --base`, got:\n{text[-2000:]}"
        )
    return body, policy


def save_policy(body: str) -> Path:
    """Write a policy to a temporary file that outlives this run, so it can be restored by hand."""
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix="opc-live-saved-policy-", suffix=".yaml", delete=False
    ) as handle:
        handle.write(body)
    return Path(handle.name)


def restore_policy(sandbox: str, saved: Path) -> bool:
    """Put back the policy saved before the probe policy was applied; on failure, say so loudly."""
    print(f"restoring the saved policy of {sandbox}...", flush=True)
    try:
        result = run(["openshell", "policy", "set", sandbox, "--policy", str(saved), "--wait"], timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        detail = str(error)
    else:
        if result.returncode == 0:
            saved.unlink(missing_ok=True)
            return True
        detail = f"exit status {result.returncode}: {result.stderr[-2000:].strip()}"
    print(
        f"\nERROR: could not restore the policy of sandbox {sandbox} ({detail}).\n"
        f"ERROR: the sandbox may still have the probe policy. Its previous policy is saved in {saved}.\n"
        f"ERROR: restore it with: openshell policy set {sandbox} --policy {saved} --wait\n",
        file=sys.stderr,
        flush=True,
    )
    return False


PROXY_REFUSED = re.compile(r"CONNECT tunnel failed, response 403")
"""curl's error when the OpenShell proxy refuses the CONNECT for a host or binary the policy does not allow."""


def curl_args(item: dict) -> list[str]:
    method = item.get("method", "GET").upper()
    args = ["curl", "-sS", "-m", "30", "-w", "\n%{http_code}"]
    # ``-X HEAD`` makes curl wait for a body that never comes; ``-I`` sends a real HEAD request.
    args += ["-I"] if method == "HEAD" else ["-X", method]
    if item.get("body") is not None:
        args += ["-H", "Content-Type: application/json", "--data", json.dumps(item["body"])]
    args.append(item["url"])
    return args


def classify(returncode: int, stdout: str, stderr: str) -> tuple[str, str, str]:
    """Return (outcome, status, detail) for one probe; the outcome is allow, deny, or error."""
    body, _, code = stdout.rstrip("\n").rpartition("\n")
    code = code.strip() if code.strip().isdigit() else "000"
    errors = (stderr + "\n" + body).strip()
    refused = PROXY_REFUSED.search(errors)
    if refused:
        return "deny", code, refused.group(0)
    if returncode != 0 or code == "000":
        return "error", code, (errors.replace("\n", " ")[-200:] or f"exit status {returncode}")
    if code == "403" and ("policy_denied" in body or "x-openshell-policy:" in body.lower()):
        return "deny", code, body.strip().replace("\n", " ")[:160]
    return "allow", code, ""


def probe(sandbox: str, item: dict) -> dict:
    args = ["openshell", "sandbox", "exec", "-n", sandbox, "--no-tty", "--timeout", "60", "--", *curl_args(item)]
    try:
        result = run(args, timeout=120, check=False)
        outcome, code, detail = classify(result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        outcome, code, detail = "error", "000", "sandbox exec timed out"
    return {
        "method": item.get("method", "GET").upper(),
        "url": item["url"],
        "expect": item["expect"],
        "outcome": outcome,
        "status": code,
        "passed": outcome == item["expect"],
        "why": item.get("why", ""),
        "detail": detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", required=True, help="Compiled policy.yaml.")
    parser.add_argument("--probes", required=True, help="YAML list of {method, url, expect: allow|deny, why}.")
    parser.add_argument(
        "--sandbox",
        help="Existing sandbox to use; its policy is replaced while the probes run, then restored. "
        "Default: create opc-live-<time> and delete it.",
    )
    parser.add_argument("--keep", action="store_true", help="Keep a sandbox this script created.")
    parser.add_argument("--results", help="Write results as JSON to this file.")
    args = parser.parse_args()

    compiled = yaml.safe_load(Path(args.policy).read_text())
    probes = yaml.safe_load(Path(args.probes).read_text())["probes"]
    created = None
    sandbox = args.sandbox
    scratch = tempfile.TemporaryDirectory(prefix="opc-live-")
    if sandbox:
        print(
            f"warning: this replaces the network policy of sandbox {sandbox} while the probes run; "
            "its current policy is saved first and restored afterwards.",
            file=sys.stderr,
            flush=True,
        )
    else:
        sandbox = created = f"opc-live-{int(time.time())}"
        print(f"creating sandbox {sandbox} (first use pulls the base image)...", flush=True)
        run(["openshell", "sandbox", "create", "--name", sandbox, "--no-auto-providers", "--no-tty", "--", "true"], timeout=900)
    saved = None
    applying = False
    restored = True
    try:
        current, merged = base_policy(sandbox)
        if not created:
            saved = save_policy(current)
            print(f"saved the current policy of {sandbox} to {saved}", flush=True)
        merged["network_policies"] = compiled.get("network_policies", {})
        merged_path = Path(scratch.name) / "applied-policy.yaml"
        merged_path.write_text(yaml.safe_dump(merged, sort_keys=False))
        print(f"applying {args.policy} to {sandbox}...", flush=True)
        applying = True
        applied = run(["openshell", "policy", "set", sandbox, "--policy", str(merged_path), "--wait"], timeout=300)
        print(applied.stdout.strip())
        results = [probe(sandbox, item) for item in probes]
    finally:
        scratch.cleanup()
        if saved is not None:
            if applying:
                restored = restore_policy(sandbox, saved)
            else:
                saved.unlink(missing_ok=True)
        if created and not args.keep:
            run(["openshell", "sandbox", "delete", created], check=False)
            print(f"deleted sandbox {created}")

    print()
    print("| Result | Expect | Outcome | Status | Request | Why |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        mark = "PASS" if r["passed"] else ("ERROR" if r["outcome"] == "error" else "FAIL")
        print(f"| {mark} | {r['expect']} | {r['outcome']} | {r['status']} | {r['method']} {r['url']} | {r['why']} |")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} probes behaved as expected.")
    for r in results:
        if r["outcome"] == "error":
            print(f"error: {r['method']} {r['url']}: {r['detail']}")
    if args.results:
        Path(args.results).write_text(json.dumps(results, indent=2) + "\n")
    if not restored:
        print(
            f"\nERROR: the policy of sandbox {sandbox} was not restored; its previous policy is saved in {saved}.",
            file=sys.stderr,
        )
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
