# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest
import yaml

from policy_compiler.cli import main

TOOL = Path(__file__).resolve().parents[1]
EXAMPLES = TOOL / "examples"
REPO_EXAMPLES = TOOL.parents[1]


def test_compile_github_explainer(tmp_path, capsys):
    out = tmp_path / "out"
    code = main(
        [
            "compile",
            str(EXAMPLES / "github-repo-explainer" / "agent.yaml"),
            "--client",
            "/usr/bin/python3",
            "--forbid",
            "github:write",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    policy = yaml.safe_load((out / "policy.yaml").read_text())
    github = policy["network_policies"]["github_rest"]
    paths = [r["allow"]["path"] for r in github["endpoints"][0]["rules"]]
    assert "/repos/NVIDIA/OpenShell/contents/**" in paths
    assert all(r["allow"]["method"] == "GET" for r in github["endpoints"][0]["rules"])
    report = json.loads((out / "report.json").read_text())
    assert report["policy"]["permitted"]["write"] == 0
    assert any(u["kind"] == "opaque_impl" for u in report["unknowns"])
    assert "summarize_architecture" in (out / "report.md").read_text()
    assert "5 allow rules" in capsys.readouterr().out


def test_compile_internal_api_with_org_catalog(tmp_path):
    example = EXAMPLES / "internal-orders-api"
    out = tmp_path / "out"
    code = main(
        [
            "compile",
            str(example / "agent.yaml"),
            "--catalog",
            str(example / "catalog"),
            "--client",
            "/usr/bin/python3",
            "--forbid",
            "acme:admin",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    endpoint = yaml.safe_load((out / "policy.yaml").read_text())["network_policies"]["acme_orders_internal_example"][
        "endpoints"
    ][0]
    assert endpoint["allowed_ips"] == ["10.20.0.0/16"]
    rules = {(r["allow"]["method"], r["allow"]["path"]) for r in endpoint["rules"]}
    assert ("POST", "/v1/orders/search") in rules
    assert not any(method == "DELETE" for method, _ in rules)
    report = json.loads((out / "report.json").read_text())
    search = next(r for r in report["rules"] if r["path"] == "/v1/orders/search")
    assert search["privilege"] == "read"  # the overlay says so, although it is a POST


def test_base_policy_keeps_filesystem(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text(
        "version: 1\nfilesystem_policy:\n  read_write: [/sandbox]\nprocess:\n  run_as_user: sandbox\n"
        "network_policies:\n  old:\n    endpoints: [{host: example.com, port: 443}]\n    binaries: [{path: /usr/bin/curl}]\n"
    )
    out = tmp_path / "out"
    main(
        [
            "compile",
            str(EXAMPLES / "github-repo-explainer" / "agent.yaml"),
            "--client",
            "/usr/bin/python3",
            "--base-policy",
            str(base),
            "--out",
            str(out),
        ]
    )
    policy = yaml.safe_load((out / "policy.yaml").read_text())
    assert policy["filesystem_policy"] == {"read_write": ["/sandbox"]}
    assert "old" not in policy["network_policies"]


def test_explain_and_lift_existing_policy(capsys):
    policy = REPO_EXAMPLES / "recipes" / "nvidia" / "pr-test-case-assistant" / "policies" / "github-api.yaml"
    assert main(["explain", str(policy)]) == 0
    text = capsys.readouterr().out
    assert "Catalogued operations reachable" in text
    assert main(["lift", str(policy)]) == 0


def test_import_openapi_and_catalog_check(tmp_path, capsys):
    example = EXAMPLES / "internal-orders-api"
    out = tmp_path / "catalog" / "acme.yaml"
    code = main(
        [
            "import",
            "openapi",
            str(example / "openapi.yaml"),
            "--overlay",
            str(example / "overlay.yaml"),
            "--provider-id",
            "acme",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    (tmp_path / "catalog" / "concepts.yaml").write_text((example / "catalog" / "acme-concepts.yaml").read_text())
    assert main(["catalog", "check", "--catalog", str(tmp_path / "catalog")]) == 0
    assert "operations 48" in capsys.readouterr().out  # 41 base + 7 imported


def test_import_openshell_policies(tmp_path, capsys):
    policy = REPO_EXAMPLES / "recipes" / "partners" / "hpe" / "retail-assistant" / "helm" / "files" / "policies"
    files = sorted(str(p) for p in policy.glob("*.yaml"))
    out = tmp_path / "mined.yaml"
    assert main(["import", "openshell-policies", *files, "--out", str(out), "--usage", str(tmp_path / "u.json")]) == 0
    usage = json.loads((tmp_path / "u.json").read_text())
    assert usage  # hosts were recorded
    assert main(["catalog", "check", "--catalog", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("gihub:write", "unknown provider"),
        ("github:unknown", "read, write, or admin"),
        ("github:WRITE", "read, write, or admin"),
        ("github", "PROVIDER:LEVEL"),
    ],
)
def test_forbid_rejects_unknown_providers_and_levels(tmp_path, value, message):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "compile",
                str(TOOL / "examples/github-repo-explainer/agent.yaml"),
                "--client",
                "/usr/bin/python3",
                "--forbid",
                value,
                "--out",
                str(tmp_path / "out"),
            ]
        )
    assert message in str(exc.value)
    assert not (tmp_path / "out").exists()


def test_report_counts_forbidden_operations_per_prohibition(tmp_path):
    out = tmp_path / "out"
    assert (
        main(
            [
                "compile",
                str(TOOL / "examples/github-repo-explainer/agent.yaml"),
                "--client",
                "/usr/bin/python3",
                "--forbid",
                "github:admin",
                "--forbid",
                "github:write",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text())
    counts = {k: len(v) for k, v in report["forbidden_by_prohibition"].items()}
    assert counts["forbid-01-github"] < counts["forbid-02-github"]
    markdown = (out / "report.md").read_text()
    assert f"| {counts['forbid-01-github']} |" in markdown
    assert f"| {counts['forbid-02-github']} |" in markdown
