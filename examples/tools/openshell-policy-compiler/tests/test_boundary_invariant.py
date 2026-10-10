# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The policy always lies within its boundary.

Every allow rule of a compiled policy must be covered by an allow rule of the
boundary on the same host and ports, for at least the same binaries, with a
method and path that admit everything the policy rule admits.
"""

import importlib.util
from pathlib import Path

import pytest

from policy_compiler import paths
from policy_compiler.backends.openshell import lift, load_policy_file
from policy_compiler.cli import main
from policy_compiler.graph.model import (
    Capability,
    CatalogDocument,
    McpSelector,
    Operation,
    Origin,
    Provider,
    Service,
)
from policy_compiler.graph.store import load_graph
from policy_compiler.ir.access import AccessRule, AccessSet, Effect
from policy_compiler.ir.common import Privilege, Protocol
from policy_compiler.ir.contract import Contract, Need, Prohibition, RawEndpoint, Target
from policy_compiler.resolve import resolve

TOOL = Path(__file__).resolve().parents[1]
PY = "/usr/bin/python3"
GIT = "/usr/bin/git"
SCOPE = {"owner": "NVIDIA", "repo": "OpenShell"}


def _example_cases() -> dict[str, list[str]]:
    spec = importlib.util.spec_from_file_location("example_cases", TOOL / "tests" / "test_examples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CASES


def _covers(broad: AccessRule, narrow: AccessRule) -> bool:
    if broad.effect != Effect.ALLOW:
        return False
    if broad.endpoint.host != narrow.endpoint.host or set(broad.endpoint.ports) != set(narrow.endpoint.ports):
        return False
    if not set(narrow.clients.paths) <= set(broad.clients.paths):
        return False
    if broad.matcher is None:
        return True
    if narrow.matcher is None:
        return False
    b, n = broad.matcher, narrow.matcher
    if b.tool is not None and b.tool != n.tool:
        return False
    if not paths.method_covers(b.method or "*", n.method or "*"):
        return False
    if b.path is None:
        return True
    return n.path is not None and paths.glob_covers(b.path, n.path)


def uncovered(policy: AccessSet, boundary: AccessSet) -> list[AccessRule]:
    return [
        rule
        for rule in policy.rules
        if rule.effect == Effect.ALLOW and not any(_covers(b, rule) for b in boundary.rules)
    ]


@pytest.mark.parametrize("expected", sorted(_example_cases()))
def test_example_policy_lies_within_its_boundary(expected, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(TOOL)
    out = tmp_path / "out"
    assert main([*_example_cases()[expected], "--out", str(out)]) == 0
    capsys.readouterr()
    policy = lift(load_policy_file(out / "policy.yaml"), source="policy.yaml")
    boundary = lift(load_policy_file(out / "boundary.yaml"), source="boundary.yaml")
    assert policy.rules
    assert uncovered(policy, boundary) == []


def _raw(nid, host, method, path, prefixes=(), **kwargs):
    return Need(
        id=nid,
        target=Target(raw=RawEndpoint(host=host, port=443, method=method, path=path, **kwargs)),
        rationale=nid,
        path_prefixes=list(prefixes),
    )


def _mcp_graph():
    doc = CatalogDocument(
        providers=[Provider(id="acme", name="Acme")],
        services=[
            Service(id="acme.mcp", provider="acme", name="mcp", origins=[Origin("mcp.acme.example")], protocol=Protocol.MCP)
        ],
        capabilities=[
            Capability(id="acme.repo:read", provider="acme", name="r", level=Privilege.READ),
            Capability(id="acme.repo:admin", provider="acme", name="a", level=Privilege.ADMIN),
        ],
        operations=[
            Operation(
                id="acme.search",
                service="acme.mcp",
                selector=McpSelector("search"),
                privilege=Privilege.READ,
                requires=[["acme.repo:read"]],
            ),
            Operation(
                id="acme.delete_repo",
                service="acme.mcp",
                selector=McpSelector("delete_repo"),
                privilege=Privilege.ADMIN,
                requires=[["acme.repo:admin"]],
            ),
        ],
    )
    return load_graph(extra_documents=[doc])


FORBID_WRITE = Prohibition(id="p", rationale="no writes", provider="github", min_privilege=Privilege.WRITE)

PROBES = {
    "uncatalogued-delete": ([_raw("n1", "api.github.com", "DELETE", "/repos/NVIDIA/OpenShell/git/refs/{ref}")], []),
    "any-method-glob": ([_raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**")], []),
    "any-method-glob-forbid-write": (
        [_raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**", prefixes=["/repos/NVIDIA/OpenShell"])],
        [FORBID_WRITE],
    ),
    "endpoint-level": ([_raw("n1", "api.github.com", None, None)], []),
    "endpoint-level-forbid-write": ([_raw("n1", "api.github.com", None, None)], [FORBID_WRITE]),
    "tcp": ([_raw("n1", "api.github.com", None, None, protocol=Protocol.TCP)], []),
    "glob-get": ([_raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/**")], []),
    "raw-contents-with-git": ([_raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/{path}")], []),
    "adjacent-placeholders": ([_raw("n1", "api.example.com", "GET", "/v1/files/{name}{ext}")], []),
    "concept-read": (
        [Need(id="n1", target=Target(concept="source-code.read", provider="github", scope=SCOPE), rationale="r")],
        [],
    ),
    "capability-read-forbid-write-capability": (
        [Need(id="n1", target=Target(capability="github.contents:read", scope=SCOPE), rationale="r")],
        [Prohibition(id="p", rationale="no", target=Target(capability="github.contents:write"))],
    ),
}


@pytest.mark.parametrize("name", sorted(PROBES))
def test_probe_policy_lies_within_its_boundary(name):
    needs, prohibitions = PROBES[name]
    contract = Contract(agent="a", default_clients=[PY, GIT], needs=needs, prohibitions=prohibitions)
    result = resolve(contract, load_graph())
    assert uncovered(result.access, result.boundary) == []


@pytest.mark.parametrize(
    "prohibitions",
    [[], [Prohibition(id="p", rationale="no admin", provider="acme", min_privilege=Privilege.ADMIN)]],
)
def test_mcp_probe_policy_lies_within_its_boundary(prohibitions):
    need = Need(
        id="n1",
        target=Target(
            raw=RawEndpoint(
                host="mcp.acme.example", port=443, protocol=Protocol.MCP, path="/mcp", mcp_tools=("search", "delete_repo")
            )
        ),
        rationale="mcp",
    )
    contract = Contract(agent="a", default_clients=[PY], needs=[need], prohibitions=prohibitions)
    result = resolve(contract, _mcp_graph())
    assert result.access.rules
    assert uncovered(result.access, result.boundary) == []
