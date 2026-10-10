# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from policy_compiler.backends.openshell import lower
from policy_compiler.graph.store import load_graph
from policy_compiler.ir.access import Effect
from policy_compiler.ir.common import Privilege, Protocol
from policy_compiler.ir.contract import Contract, Locus, Need, Phase, Prohibition, RawEndpoint, Target
from policy_compiler.resolve import ResolveOptions, resolve

PY = "/usr/bin/python3"
GIT = "/usr/bin/git"


@pytest.fixture(scope="module")
def graph():
    return load_graph()


def raw_need(nid, host, method, path, **kwargs):
    return Need(
        id=nid,
        target=Target(raw=RawEndpoint(host=host, port=443, method=method, path=path, **kwargs)),
        rationale=nid,
    )


def allows(access):
    return sorted(
        (r.endpoint.host, r.matcher.method, r.matcher.path, r.clients.paths)
        for r in access.rules
        if r.effect == Effect.ALLOW and r.matcher is not None and r.matcher.path is not None
    )


def test_agentspec_style_raw_needs_keep_declared_literals(graph):
    contract = Contract(
        agent="explainer",
        default_clients=[PY],
        needs=[
            raw_need("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell"),
            raw_need("n2", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/{path}"),
        ],
    )
    result = resolve(contract, graph)
    assert allows(result.access) == [
        ("api.github.com", "GET", "/repos/NVIDIA/OpenShell", (PY,)),
        ("api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/**", (PY,)),
    ]
    assert [n.status for n in result.needs] == ["resolved", "resolved"]
    assert result.needs[1].operations == ["github.repos.get-content"]
    policy = lower(result.access)
    endpoint = policy["network_policies"]["github_rest"]["endpoints"][0]
    assert endpoint["protocol"] == "rest"
    assert endpoint["enforcement"] == "enforce"
    assert policy["network_policies"]["github_rest"]["binaries"] == [{"path": PY}]


def test_concept_need_pins_scope_and_routes_git_operations_to_git(graph):
    contract = Contract(
        agent="reader",
        default_clients=[PY, GIT],
        needs=[
            Need(
                id="n1",
                target=Target(
                    concept="source-code.read", provider="github", scope={"owner": "NVIDIA", "repo": "OpenShell"}
                ),
                rationale="read code",
            )
        ],
    )
    result = resolve(contract, graph)
    rules = allows(result.access)
    git_rules = [r for r in rules if r[3] == (GIT,)]
    py_rules = [r for r in rules if r[3] == (PY,)]
    assert ("github.com", "POST", "/NVIDIA/OpenShell.git/git-upload-pack", (GIT,)) in git_rules
    assert all(r[0] != "github.com" for r in py_rules)  # python never gets git smart HTTP
    assert ("api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/**", (PY,)) in py_rules
    assert all("NVIDIA/OpenShell" in r[2] for r in rules)  # everything is scoped to the repo
    assert not any(r[1] in {"PUT", "DELETE"} for r in rules)
    note = " ".join(result.needs[0].notes)
    assert "github.markdown.render" in note  # cannot be scoped, so left out


def test_boundary_drops_scope(graph):
    contract = Contract(
        agent="reader",
        default_clients=[PY],
        needs=[
            Need(
                id="n1",
                target=Target(concept="source-code.read", provider="github", scope={"owner": "NVIDIA", "repo": "x"}),
                rationale="read",
            )
        ],
    )
    result = resolve(contract, graph)
    boundary = allows(result.boundary)
    assert ("api.github.com", "GET", "/repos/*/*/contents/**", (PY,)) in boundary
    assert not any(r[1] in {"PUT", "DELETE"} for r in boundary)


def test_prohibition_conflict_removes_grant(graph):
    contract = Contract(
        agent="writer",
        default_clients=[PY],
        needs=[raw_need("n1", "api.github.com", "PUT", "/repos/NVIDIA/OpenShell/contents/{path}")],
        prohibitions=[Prohibition(id="p1", rationale="read only", provider="github", min_privilege=Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    assert result.access.rules == []
    assert result.needs[0].status == "conflict"
    assert any(d.code == "prohibited" for d in result.access.diagnostics)


def test_prohibition_adds_deny_under_broad_allow(graph):
    contract = Contract(
        agent="broad",
        default_clients=[PY],
        needs=[raw_need("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**")],
        prohibitions=[Prohibition(id="p1", rationale="no writes", target=Target(capability="github.contents:write"))],
    )
    result = resolve(contract, graph)
    denies = sorted(
        (r.matcher.method, r.matcher.path) for r in result.access.rules if r.effect == Effect.DENY
    )
    assert ("DELETE", "/repos/*/*/contents/**") in denies
    assert ("PUT", "/repos/*/*/contents/**") in denies
    policy = lower(result.access)
    assert policy["network_policies"]["github_rest"]["endpoints"][0]["deny_rules"]


def test_covered_matchers_are_pruned(graph):
    contract = Contract(
        agent="prune",
        default_clients=[PY],
        needs=[
            raw_need("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/**"),
            raw_need("n2", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/{path}"),
        ],
    )
    result = resolve(contract, graph)
    assert allows(result.access) == [("api.github.com", "GET", "/repos/NVIDIA/OpenShell/**", (PY,))]
    assert set(result.access.rules[0].reasons) == {"n1", "n2"}


def test_uncatalogued_raw_need_follows_declared_shape(graph):
    contract = Contract(
        agent="x", default_clients=[PY], needs=[raw_need("n1", "api.example.com", "GET", "/v1/items/{id}")]
    )
    result = resolve(contract, graph)
    assert allows(result.access) == [("api.example.com", "GET", "/v1/items/*", (PY,))]
    assert result.needs[0].status == "uncatalogued"
    assert result.access.rules[0].privilege == Privilege.READ


def test_private_host_warns_about_allowed_ips(graph):
    contract = Contract(
        agent="x", default_clients=[PY], needs=[raw_need("n1", "orders.internal.example", "GET", "/v1/orders")]
    )
    result = resolve(contract, graph)
    assert any(d.code == "private-destination" for d in result.access.diagnostics)


def test_mcp_need_allows_lifecycle_and_named_tools(graph):
    need = Need(
        id="n1",
        target=Target(
            raw=RawEndpoint(host="mcp.example.com", port=443, protocol=Protocol.MCP, path="/mcp", mcp_tools=("search",))
        ),
        rationale="mcp",
    )
    result = resolve(Contract(agent="x", default_clients=[PY], needs=[need]), graph)
    methods = sorted((r.matcher.method, r.matcher.tool or "") for r in result.access.rules)
    assert methods == [
        ("initialize", ""),
        ("notifications/initialized", ""),
        ("tools/call", "search"),
        ("tools/list", ""),
    ]
    endpoint = lower(result.access)["network_policies"]["mcp_example_com"]["endpoints"][0]
    assert endpoint["protocol"] == "mcp"


def test_skipped_needs(graph):
    contract = Contract(
        agent="x",
        default_clients=[PY],
        needs=[
            Need(id="n1", target=Target(capability="github.contents:read"), rationale="", locus=Locus.CLIENT),
            Need(id="n2", target=Target(capability="github.contents:read"), rationale="", phase=Phase.INSTALL),
        ],
    )
    result = resolve(contract, graph)
    assert [n.status for n in result.needs] == ["skipped", "skipped"]
    assert result.access.rules == []


def test_missing_clients_is_an_error(graph):
    result = resolve(Contract(agent="x", needs=[raw_need("n1", "api.github.com", "GET", "/zen")]), graph)
    assert result.needs[0].status == "unresolved"
    assert any(d.code == "no-clients" for d in result.access.diagnostics)


def test_route_inference_local(graph):
    contract = Contract(
        agent="x",
        default_clients=[PY],
        needs=[raw_need("n1", "integrate.api.nvidia.com", "POST", "/v1/chat/completions")],
    )
    result = resolve(contract, graph, ResolveOptions(route_inference_local=True))
    assert allows(result.access) == [("inference.local", "POST", "/v1/chat/completions", (PY,))]
    assert "inference.local" in " ".join(result.needs[0].notes)


def test_wildcard_method_operation_does_not_widen_declared_method():
    from policy_compiler.graph.model import CatalogDocument, Operation, Origin, Provider, RestSelector, Service

    doc = CatalogDocument(
        providers=[Provider(id="mined", name="Mined")],
        services=[Service(id="mined.api", provider="mined", name="api", origins=[Origin("api.mined.example")])],
        operations=[
            Operation(
                id="mined.api.any",
                service="mined.api",
                selector=RestSelector(method="*", path="/v1/{rest}"),
                privilege=Privilege.UNKNOWN,
                multi_segment=["rest"],
            )
        ],
    )
    graph = load_graph(extra_documents=[doc])
    contract = Contract(agent="x", default_clients=[PY], needs=[raw_need("n1", "api.mined.example", "GET", "/v1/a/b")])
    result = resolve(contract, graph)
    assert result.needs[0].operations == ["mined.api.any"]  # matched the catalogued operation
    assert [(r.matcher.method, r.matcher.path) for r in result.access.rules] == [("GET", "/v1/a/b")]
