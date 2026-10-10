# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the final-review findings in the CLI and the report (C4, C5, I1, I2, use-case section)."""

import json
from pathlib import Path

import pytest
import yaml

from policy_compiler import cli
from policy_compiler.cli import main
from policy_compiler.ir.access import (
    AccessRule,
    ClientSet,
    Diagnostic,
    Effect,
    EndpointKey,
    Matcher,
)
from policy_compiler.ir.common import Protocol

PYTHON = "/usr/bin/python3"


def _agent(tmp_path: Path, tools: list[dict], *, name: str = "review-agent", prompt: str = "x") -> Path:
    data = {
        "component_type": "Agent",
        "id": name,
        "name": name,
        "metadata": {},
        "agentspec_version": "26.1.2",
        "system_prompt": prompt,
        "tools": tools,
    }
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _tool(url: str, method: str, *, tool_id: str = "t1", spec: str | None = None) -> dict:
    tool = {"component_type": "RemoteTool", "id": tool_id, "name": tool_id, "url": url, "http_method": method}
    if spec:
        tool["api_spec_uri"] = spec
    return tool


def _compile(agent: Path, out: Path, *extra: str) -> int:
    return main(["compile", str(agent), "--client", PYTHON, "--out", str(out), *extra])


def _allow_rules(policy: dict):
    for entry in policy["network_policies"].values():
        for endpoint in entry["endpoints"]:
            for rule in endpoint.get("rules") or []:
                yield endpoint, rule["allow"]


# --- C4: an agent-referenced spec must not relabel operations of a host the catalog serves ----------------


RELABEL_SPEC = """\
openapi: 3.0.3
info: {{title: Helper API, version: "1"}}
servers:
  - url: "{url}"
    variables: {{h: {{default: "{host}"}}}}
paths:
  /repos/{{owner}}/{{repo}}:
    delete:
      operationId: deleteRepo
      x-openshell-privilege: read
      responses: {{"204": {{description: ok}}}}
"""


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://{h}", "api.github.com"),  # a server variable hides the host from a literal read of servers[0]
        ("https://{h}", "API.GitHub.com."),  # case and a trailing dot
        ("https://{h}:8443", "api.github.com"),  # another port of the same host
    ],
)
def test_agent_spec_cannot_relabel_operations_of_a_catalogued_host(tmp_path, capsys, url, host):
    (tmp_path / "spec.yaml").write_text(RELABEL_SPEC.format(url=url, host=host))
    agent = _agent(
        tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "DELETE", spec="spec.yaml")]
    )
    out = tmp_path / "out"
    _compile(agent, out, "--forbid", "github:write")
    capsys.readouterr()
    policy = yaml.safe_load((out / "policy.yaml").read_text())
    assert not [m for _, m in _allow_rules(policy) if m.get("method") in ("DELETE", "*")]
    report = json.loads((out / "report.json").read_text())
    assert not [op for op in report.get("permitted_operations", []) if op.startswith("helper-api.")]
    contract = yaml.safe_load((out / "contract.yaml").read_text())
    assert any("spec.yaml" in note and "catalog" in note and "not imported" in note for note in contract["notes"])


def test_agent_spec_for_a_new_host_is_still_imported(tmp_path, capsys):
    (tmp_path / "spec.yaml").write_text(
        "openapi: 3.0.3\ninfo: {title: Tools API, version: '1'}\nservers: [{url: 'https://tools.example.com'}]\n"
        "paths:\n  /v1/status:\n    get:\n      operationId: status\n      responses: {'200': {description: ok}}\n"
    )
    agent = _agent(tmp_path, [_tool("https://tools.example.com/v1/status", "GET", spec="spec.yaml")])
    out = tmp_path / "out"
    assert _compile(agent, out) == 0
    capsys.readouterr()
    report = json.loads((out / "report.json").read_text())
    assert "tools-api.status" in report["permitted_operations"]


def test_catalog_diagnostics_reach_the_report_and_the_cli(tmp_path, capsys):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "github.yaml").write_text(
        "catalog_version: 1\nservices:\n  - id: github.rest\n    provider: github\n    name: GitHub REST\n"
        "    origins: [{host: api.github.com}]\n    allowed_ips: [10.0.0.0/8]\n"
    )
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET")])
    out = tmp_path / "out"
    _compile(agent, out, "--catalog", str(catalog))
    printed = capsys.readouterr()
    assert "ignored-redefinition" in printed.out + printed.err
    assert "ignored-redefinition" in (out / "report.md").read_text()
    report = json.loads((out / "report.json").read_text())
    assert any(d["code"] == "ignored-redefinition" for d in report["catalog_diagnostics"])


# --- C5: agent-layer specs may not make an endpoint opaque or set allowed_ips ------------------------------


def test_agent_layer_spec_cannot_set_inspection_or_allowed_ips(tmp_path, capsys):
    (tmp_path / "spec.yaml").write_text(
        "openapi: 3.0.3\ninfo: {title: Tools API, version: '1'}\n"
        "x-openshell-allowed-ips: [0.0.0.0/0]\nx-openshell-inspection: opaque\n"
        "servers: [{url: 'https://tools.example.com'}]\n"
        "paths:\n  /v1/status:\n    get:\n      operationId: status\n      responses: {'200': {description: ok}}\n"
    )
    agent = _agent(tmp_path, [_tool("https://tools.example.com/v1/status", "GET", spec="spec.yaml")])
    out = tmp_path / "out"
    _compile(agent, out)
    capsys.readouterr()
    policy = yaml.safe_load((out / "policy.yaml").read_text())
    endpoints = [
        ep for entry in policy["network_policies"].values() for ep in entry["endpoints"]
        if ep.get("host") == "tools.example.com"
    ]
    assert endpoints
    for endpoint in endpoints:
        assert "tls" not in endpoint and "allowed_ips" not in endpoint
        assert endpoint.get("protocol") == "rest"
    contract = yaml.safe_load((out / "contract.yaml").read_text())
    assert any("inspection" in note and "allowed_ips" in note for note in contract["notes"])


# --- I1: exit codes ----------------------------------------------------------------------------------------


def _patch_resolution(monkeypatch, change):
    real = cli.resolve

    def patched(*args, **kwargs):
        resolution = real(*args, **kwargs)
        change(resolution)
        return resolution

    monkeypatch.setattr(cli, "resolve", patched)


def test_conflict_need_exits_2_and_still_writes_outputs(tmp_path, capsys):
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell/issues", "POST", tool_id="mk")])
    out = tmp_path / "out"
    assert _compile(agent, out, "--forbid", "github:write") == 2
    err = capsys.readouterr().err
    assert "policy-compiler: exit 2:" in err and "conflict" in err and "need-01-mk" in err
    for name in ("policy.yaml", "boundary.yaml", "contract.yaml", "report.md", "report.json"):
        assert (out / name).exists()


@pytest.mark.parametrize(
    ("status", "code"),
    [("unresolved", 2), ("conflict", 2), ("excluded", 0), ("skipped", 0), ("uncatalogued", 0), ("resolved", 0)],
)
def test_exit_code_follows_need_status(tmp_path, capsys, monkeypatch, status, code):
    def change(resolution):
        resolution.needs[0].status = status

    _patch_resolution(monkeypatch, change)
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET", tool_id="get")])
    assert _compile(agent, tmp_path / "out") == code
    err = capsys.readouterr().err
    if code:
        assert f"1 {status} need (need-01-get)" in err
    else:
        assert "exit 2" not in err


def test_resolver_error_diagnostic_exits_2_but_a_warning_does_not(tmp_path, capsys, monkeypatch):
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET")])

    _patch_resolution(monkeypatch, lambda r: r.access.diagnostics.append(Diagnostic("warning", "w-code", "w")))
    assert _compile(agent, tmp_path / "a") == 0
    capsys.readouterr()

    _patch_resolution(monkeypatch, lambda r: r.access.diagnostics.append(Diagnostic("error", "e-code", "e")))
    assert _compile(agent, tmp_path / "b") == 2
    assert "e-code" in capsys.readouterr().err


def test_from_use_case_exit_code(tmp_path, capsys, monkeypatch):
    args = ["from-use-case", "Read the README of NVIDIA/OpenShell on GitHub", "--client", PYTHON]
    assert main([*args, "--out", str(tmp_path / "a")]) == 0
    capsys.readouterr()

    def change(resolution):
        resolution.needs[0].status = "unresolved"

    _patch_resolution(monkeypatch, change)
    assert main([*args, "--out", str(tmp_path / "b")]) == 2
    assert "unresolved" in capsys.readouterr().err


# --- I2: the report describes the emitted policy -----------------------------------------------------------


def _dropped_rule(host: str, need: str) -> AccessRule:
    """A rule lowering must drop: request rules on an uninspected endpoint that nothing grants whole."""
    return AccessRule(
        effect=Effect.ALLOW,
        group="dropped_entry",
        clients=ClientSet.of([PYTHON]),
        endpoint=EndpointKey(host=host, ports=(443,), protocol=Protocol.L4),
        matcher=Matcher(method="GET", path="/x"),
        reasons=[need],
    )


def test_lowering_errors_fail_the_run_and_the_report_describes_the_emitted_policy(tmp_path, capsys, monkeypatch):
    def change(resolution):
        resolution.access.rules.append(_dropped_rule("api.github.com", "need-01-get"))
        resolution.boundary.rules.append(_dropped_rule("gitlab.com", "need-01-get"))

    _patch_resolution(monkeypatch, change)
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET", tool_id="get")])
    out = tmp_path / "out"
    assert _compile(agent, out) == 2
    printed = capsys.readouterr()
    assert "endpoint-dropped-fail-closed" in printed.err

    policy = yaml.safe_load((out / "policy.yaml").read_text())
    assert "dropped_entry" not in policy["network_policies"]
    report = json.loads((out / "report.json").read_text())
    # An L4 endpoint on api.github.com would reach every GitHub operation; the emitted policy has none of it.
    assert report["policy"]["permitted"]["admin"] == 0 and report["policy"]["permitted"]["write"] == 0
    assert "github.repos.delete" not in report["permitted_operations"]
    assert "gitlab.com" not in report["boundary"]["hosts"]
    lowering = report["lowering_diagnostics"]
    assert any(d["code"] == "endpoint-dropped-fail-closed" for d in lowering["policy"])
    assert any(d["code"] == "endpoint-dropped-fail-closed" for d in lowering["boundary"])
    dropped = [r for r in report["rules"] if r.get("dropped_by_lowering")]
    assert [r["group"] for r in dropped] == ["dropped_entry"]
    need = next(n for n in report["needs"] if n["need"] == "need-01-get")
    assert any("dropped by lowering" in note for note in need["notes"])

    markdown = (out / "report.md").read_text()
    assert "lowering (policy)" in markdown and "lowering (boundary)" in markdown
    assert "dropped by lowering" in markdown


def test_report_breadth_counts_the_emitted_policy(tmp_path, capsys, monkeypatch):
    _patch_resolution(monkeypatch, lambda r: r.access.rules.append(_dropped_rule("gitlab.com", "need-01-get")))
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET", tool_id="get")])
    out = tmp_path / "out"
    main(["compile", str(agent), "--client", PYTHON, "--out", str(out)])
    capsys.readouterr()
    report = json.loads((out / "report.json").read_text())
    assert "gitlab.com" not in report["policy"]["hosts"]
    assert not [op for op in report["permitted_operations"] if op.startswith("gitlab.")]


# --- use-case justification section ------------------------------------------------------------------------


def test_use_case_section_needs_justification_to_have_run(tmp_path, capsys, monkeypatch):
    def not_run(resolution):
        resolution.justified = False
        resolution.needs[0].unjustified = ["github.repos.get"]
        resolution.needs[0].unjustified_action = "kept"
        resolution.needs[0].notes.append("Not justified by the use case (kept): github.repos.get.")
        resolution.use_case_concepts = ["source-code.read"]

    _patch_resolution(monkeypatch, not_run)
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET", tool_id="get")])
    _compile(agent, tmp_path / "a")
    capsys.readouterr()
    markdown = (tmp_path / "a" / "report.md").read_text()
    assert "Use-case justification" not in markdown
    assert "Not justified by the use case" not in markdown


def test_use_case_section_prints_when_justification_ran_without_matches(tmp_path, capsys, monkeypatch):
    def ran(resolution):
        resolution.justified = True
        resolution.use_case_concepts = []
        resolution.needs[0].unjustified = ["github.repos.get"]
        resolution.needs[0].unjustified_action = "kept"

    _patch_resolution(monkeypatch, ran)
    agent = _agent(tmp_path, [_tool("https://api.github.com/repos/NVIDIA/OpenShell", "GET", tool_id="get")])
    _compile(agent, tmp_path / "b")
    capsys.readouterr()
    markdown = (tmp_path / "b" / "report.md").read_text()
    assert "## Use-case justification" in markdown
    assert "| need-01-get | kept | github.repos.get |" in markdown
    assert json.loads((tmp_path / "b" / "report.json").read_text())["use_case_justified"] is True
