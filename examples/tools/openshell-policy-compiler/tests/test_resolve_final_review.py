# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the final review of the resolver.

Each test reproduces a finding where the generated policy allowed more than the
agent declared, or where a ``--forbid`` prohibition did not hold.
"""

import pytest

from policy_compiler import paths
from policy_compiler.backends.openshell import lower
from policy_compiler.graph.model import (
    Capability,
    CatalogDocument,
    McpSelector,
    Operation,
    Origin,
    Provider,
    RestSelector,
    Service,
)
from policy_compiler.graph.store import load_graph
from policy_compiler.ir.access import Effect
from policy_compiler.ir.common import Evidence, EvidenceKind, Inspection, Layer, Privilege, Protocol
from policy_compiler.ir.contract import Contract, Credential, Need, Prohibition, RawEndpoint, Target
from policy_compiler.resolve import ResolveOptions, resolve
from policy_compiler.semantic import SemanticIndex

PY = "/usr/bin/python3"
GIT = "/usr/bin/git"
SCOPE = {"owner": "NVIDIA", "repo": "OpenShell"}
LLM_EVIDENCE = [Evidence(EvidenceKind.DECLARED, "agentspec:agent.yaml", pointer="$.llm_config")]


@pytest.fixture(scope="module")
def graph():
    return load_graph()


@pytest.fixture(scope="module")
def acme_graph():
    """The reviewer's catalog: an opaque vault and an MCP tools server, both ACME's."""
    doc = CatalogDocument(
        providers=[Provider(id="acme", name="ACME")],
        services=[
            Service(
                id="acme.vault",
                provider="acme",
                name="Vault",
                origins=[Origin("vault.acme.example")],
                inspection=Inspection.OPAQUE,
            ),
            Service(
                id="acme.tools",
                provider="acme",
                name="Tools MCP",
                origins=[Origin("tools.acme.example")],
                protocol=Protocol.MCP,
            ),
        ],
        capabilities=[Capability(id="acme.tools:read", provider="acme", name="read", level=Privilege.READ)],
        operations=[
            Operation(
                id="acme.vault.read",
                service="acme.vault",
                selector=RestSelector("GET", "/v1/secret/{name}"),
                privilege=Privilege.READ,
            ),
            Operation(
                id="acme.tools.search",
                service="acme.tools",
                selector=McpSelector("search"),
                privilege=Privilege.READ,
                requires=[["acme.tools:read"]],
            ),
        ],
    )
    return load_graph(extra_documents=[doc])


def raw(nid, host, method, path, prefixes=(), port=443, evidence=(), credential=None, **kwargs):
    return Need(
        id=nid,
        target=Target(raw=RawEndpoint(host=host, port=port, method=method, path=path, **kwargs)),
        rationale=nid,
        path_prefixes=list(prefixes),
        evidence=list(evidence),
        credential=credential,
    )


def mcp(nid, host, path, tools=("search",)):
    return Need(
        id=nid,
        target=Target(raw=RawEndpoint(host=host, port=443, protocol=Protocol.MCP, path=path, mcp_tools=tuple(tools))),
        rationale=nid,
    )


def forbid(provider, level):
    return Prohibition(id="p", rationale="no", provider=provider, min_privilege=level)


def compile_(graph, needs, prohibitions=(), clients=(PY,), options=None, index=None, use_case=""):
    contract = Contract(
        agent="a", default_clients=list(clients), needs=needs, prohibitions=list(prohibitions), use_case=use_case
    )
    return resolve(contract, graph, options, concept_index=index)


def allow_rules(access, host=None):
    return [r for r in access.rules if r.effect == Effect.ALLOW and (host is None or r.endpoint.host == host)]


def need_result(result, need_id="n1"):
    return next(n for n in result.needs if n.need == need_id)


def notes(result, need_id="n1"):
    return " ".join(need_result(result, need_id).notes)


def codes(result):
    return [d.code for d in result.access.diagnostics]


# ---------------------------------------------------------------- C1: glob characters and methods


@pytest.mark.parametrize(
    "path",
    ["/repos/NVIDIA/OpenShell/**", "/repos/*/OpenShell", "/repos/NVIDIA/Open?hell", "/repos/NVIDIA/[O]penShell"],
)
def test_glob_characters_in_a_declared_literal_path_are_not_trusted(graph, path):
    result = compile_(graph, [raw("n1", "api.github.com", "GET", path)])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "unresolved"
    assert "glob" in notes(result)


def test_compiler_built_glob_path_is_trusted(graph):
    need = raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/**", path_is_glob=True)
    result = compile_(graph, [need])
    assert [(r.matcher.method, r.matcher.path) for r in allow_rules(result.access)] == [
        ("GET", "/repos/NVIDIA/OpenShell/**")
    ]


@pytest.mark.parametrize("method", ["CONNECT", "TRACE", "PROPFIND", "GET POST", "tools/call", ""])
def test_non_standard_method_is_unresolved(graph, method):
    result = compile_(graph, [raw("n1", "api.example.com", method, "/v1/items")])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "unresolved"
    assert "method" in notes(result)


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "*"])
def test_standard_methods_are_accepted(graph, method):
    result = compile_(graph, [raw("n1", "api.example.com", method, "/v1/items")])
    assert [(r.matcher.method, r.matcher.path) for r in allow_rules(result.access)] == [(method, "/v1/items")]


def test_endpoint_level_need_is_still_accepted(graph):
    result = compile_(graph, [raw("n1", "api.example.com", None, None)])
    assert [(r.matcher.method, r.matcher.path) for r in allow_rules(result.access)] == [("*", "/**")]


def test_mcp_tool_name_with_glob_characters_is_not_granted(graph):
    result = compile_(graph, [mcp("n1", "gitlab.com", "/api/v4/mcp", tools=("get_issue", "*"))])
    tools = {r.matcher.tool for r in allow_rules(result.access) if r.matcher.method == "tools/call"}
    assert tools == {"get_issue"}
    assert "glob" in notes(result)


def test_mcp_path_with_glob_characters_is_unresolved(graph):
    result = compile_(graph, [mcp("n1", "mcp.example.com", "/mcp/**")])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "unresolved"


# ---------------------------------------------------------------- C2: MCP endpoint path


def test_mcp_rules_carry_the_declared_server_path(graph):
    result = compile_(graph, [mcp("n1", "gitlab.com", "/api/v4/mcp", tools=("get_issue",))])
    rules = allow_rules(result.access)
    assert rules and {r.endpoint.path for r in rules} == {"/api/v4/mcp"}
    endpoint = next(
        e for group in lower(result.access)["network_policies"].values() for e in group["endpoints"] if e.get("protocol") == "mcp"
    )
    assert endpoint["path"] == "/api/v4/mcp"


def test_mcp_sse_transport_covers_its_directory_and_records_a_widening(graph):
    result = compile_(graph, [mcp("n1", "mcp.example.com", "/v1/sse")])
    assert {r.endpoint.path for r in allow_rules(result.access)} == {"/v1/**"}
    assert ("n1", "mcp-sse-path") in [(w.need, w.reason) for w in result.access.widenings]


def test_mcp_without_a_declared_path_is_pathless(graph):
    result = compile_(graph, [mcp("n1", "mcp.example.com", None)])
    rules = allow_rules(result.access)
    assert rules and {r.endpoint.path for r in rules} == {""}


def test_mcp_path_keeps_a_forbidden_rest_call_on_the_same_host_out(acme_graph):
    """xmod/opaque.yaml: an MCP toolbox and a REST POST on the same host, with --forbid acme:write."""
    needs = [mcp("n1", "tools.acme.example", "/mcp"), raw("n2", "tools.acme.example", "POST", "/api/things")]
    result = compile_(acme_graph, needs, [forbid("acme", Privilege.WRITE)])
    rules = allow_rules(result.access, "tools.acme.example")
    assert rules and all(r.endpoint.protocol == Protocol.MCP for r in rules)
    # A path-less MCP endpoint would admit POST /api/things with a JSON-RPC body.
    assert {r.endpoint.path for r in rules} == {"/mcp"}
    assert need_result(result, "n2").status == "conflict"


def test_catalogued_mcp_grants_without_a_declared_path_stay_pathless(acme_graph):
    need = Need(id="n1", target=Target(capability="acme.tools:read"), rationale="search")
    result = compile_(acme_graph, [need])
    rules = allow_rules(result.access, "tools.acme.example")
    assert rules and {r.endpoint.path for r in rules} == {""}


def test_mcp_lifecycle_is_dropped_with_its_last_tool_per_path(acme_graph):
    need = mcp("n1", "tools.acme.example", "/mcp", tools=("delete_everything",))
    result = compile_(acme_graph, [need], [forbid("acme", Privilege.WRITE)])
    assert allow_rules(result.access) == []


# ---------------------------------------------------------------- C3: ports and schemes


def test_provider_prohibition_holds_on_an_uncatalogued_port(graph):
    need = raw("n1", "api.github.com", "POST", "/repos/NVIDIA/OpenShell/issues", port=80, scheme="http")
    result = compile_(graph, [need], [forbid("github", Privilege.WRITE)])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_provider_prohibition_removes_connection_level_access_on_any_port(graph):
    need = raw("n1", "api.github.com", None, None, port=8443, protocol=Protocol.TCP)
    result = compile_(graph, [need], [forbid("github", Privilege.WRITE)])
    assert allow_rules(result.access) == []


@pytest.mark.parametrize(("port", "scheme"), [(80, "http"), (8443, "https"), (443, "http")])
def test_catalogued_host_on_an_uncatalogued_port_or_scheme_warns(graph, port, scheme):
    need = raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell", port=port, scheme=scheme)
    result = compile_(graph, [need])
    assert "uncatalogued-port" in codes(result)
    assert any(d.level == "warning" for d in result.access.diagnostics if d.code == "uncatalogued-port")


def test_catalogued_origin_does_not_warn(graph):
    result = compile_(graph, [raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell")])
    assert "uncatalogued-port" not in codes(result)


def test_raw_prohibition_matches_the_host_on_any_port(graph):
    prohibition = Prohibition(id="p", rationale="never", target=Target(raw=RawEndpoint(host="evil.example.com", port=443)))
    need = raw("n1", "evil.example.com", "GET", "/x", port=8443)
    result = compile_(graph, [need], [prohibition])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_raw_prohibition_forbids_catalogued_operations_on_any_port(graph):
    prohibition = Prohibition(id="p", rationale="never", target=Target(raw=RawEndpoint(host="api.github.com", port=8080)))
    result = compile_(graph, [], [prohibition])
    assert "github.repos.get" in result.forbidden_operations


def test_deny_rules_apply_on_an_uncatalogued_port(graph):
    need = raw("n1", "api.github.com", None, None, port=80, scheme="http")
    prohibition = Prohibition(id="p", rationale="no", target=Target(capability="github.contents:write"))
    result = compile_(graph, [need], [prohibition])
    denies = {(r.matcher.method, r.matcher.path) for r in result.access.rules if r.effect == Effect.DENY}
    assert ("PUT", "/repos/*/*/contents/**") in denies


# ---------------------------------------------------------------- C4: method privilege under a provider floor


def _agent_layer_graph():
    """An agent-referenced spec that labels a DELETE on the GitHub host as read."""
    doc = CatalogDocument(
        providers=[Provider(id="helper", name="Helper", layer=Layer.AGENT)],
        services=[
            Service(
                id="helper.api",
                provider="helper",
                name="Helper API",
                origins=[Origin("api.github.com")],
                layer=Layer.AGENT,
            )
        ],
        operations=[
            Operation(
                id="helper.delete-thing",
                service="helper.api",
                selector=RestSelector("DELETE", "/repos/{owner}/{repo}/thing"),
                privilege=Privilege.READ,
                layer=Layer.AGENT,
            )
        ],
    )
    return load_graph(extra_documents=[doc])


def test_agent_layer_read_label_does_not_bypass_a_provider_floor():
    graph = _agent_layer_graph()
    need = raw("n1", "api.github.com", "DELETE", "/repos/NVIDIA/OpenShell/thing")
    unforbidden = compile_(graph, [need])
    assert need_result(unforbidden).operations == ["helper.delete-thing"]
    result = compile_(graph, [need], [forbid("github", Privilege.WRITE)])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_agent_layer_operation_is_escalated_by_its_method_in_the_forbidden_set():
    graph = _agent_layer_graph()
    result = compile_(graph, [], [forbid("helper", Privilege.WRITE)])
    assert "helper.delete-thing" in result.forbidden_by["p"]


def test_wildcard_selector_operation_is_judged_by_the_declared_method():
    doc = CatalogDocument(
        providers=[Provider(id="mined", name="Mined")],
        services=[Service(id="mined.api", provider="mined", name="api", origins=[Origin("api.mined.example")])],
        operations=[
            Operation(
                id="mined.api.any",
                service="mined.api",
                selector=RestSelector(method="*", path="/v1/{rest}"),
                privilege=Privilege.READ,
                multi_segment=["rest"],
            )
        ],
    )
    graph = load_graph(extra_documents=[doc])
    need = raw("n1", "api.mined.example", "DELETE", "/v1/a/b")
    result = compile_(graph, [need], [forbid("mined", Privilege.WRITE)])
    assert allow_rules(result.access) == []
    get = compile_(graph, [raw("n1", "api.mined.example", "GET", "/v1/a/b")], [forbid("mined", Privilege.WRITE)])
    assert [(r.matcher.method, r.matcher.path) for r in allow_rules(get.access)] == [("GET", "/v1/a/b")]


def test_curated_read_level_post_survives_a_write_floor(graph):
    """The base catalog's curated privileges hold: git fetch (POST git-upload-pack) is a read."""
    need = Need(id="n1", target=Target(concept="source-code.read", provider="github", scope=SCOPE), rationale="r")
    result = compile_(graph, [need], [forbid("github", Privilege.WRITE)], clients=(PY, GIT))
    assert any(
        r.matcher.path == "/NVIDIA/OpenShell.git/git-upload-pack" and GIT in r.clients.paths
        for r in allow_rules(result.access, "github.com")
    )


def test_mcp_grants_are_not_judged_by_http_method(acme_graph):
    result = compile_(acme_graph, [mcp("n1", "tools.acme.example", "/mcp")], [forbid("acme", Privilege.WRITE)])
    tools = {(r.matcher.method, r.matcher.tool) for r in allow_rules(result.access)}
    assert ("tools/call", "search") in tools


# ---------------------------------------------------------------- C5: broad allowed_ips


def _ips_graph(cidrs):
    doc = CatalogDocument(
        providers=[Provider(id="corp", name="Corp")],
        services=[
            Service(
                id="corp.db",
                provider="corp",
                name="db",
                origins=[Origin("db.corp.example")],
                allowed_ips=list(cidrs),
            )
        ],
        operations=[
            Operation(
                id="corp.db.get",
                service="corp.db",
                selector=RestSelector("GET", "/v1/rows"),
                privilege=Privilege.READ,
            )
        ],
    )
    return load_graph(extra_documents=[doc])


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "::/0", "10.0.0.0/7", "2000::/3"])
def test_broad_allowed_ips_warn(cidr):
    graph = _ips_graph([cidr])
    need = Need(id="n1", target=Target(operation="corp.db.get"), rationale="r")
    result = compile_(graph, [need])
    found = [d for d in result.access.diagnostics if d.code == "broad-allowed-ips"]
    assert found and all(d.level == "warning" for d in found)
    assert cidr in found[0].message


@pytest.mark.parametrize("cidr", ["10.0.0.0/8", "10.20.0.0/16", "fd00::/8"])
def test_narrow_allowed_ips_do_not_warn(cidr):
    graph = _ips_graph([cidr])
    need = Need(id="n1", target=Target(operation="corp.db.get"), rationale="r")
    result = compile_(graph, [need])
    assert "broad-allowed-ips" not in codes(result)


def test_broad_allowed_ips_in_the_boundary_warn():
    doc = CatalogDocument(
        providers=[Provider(id="corp", name="Corp")],
        services=[
            Service(id="corp.api", provider="corp", name="api", origins=[Origin("api.corp.example")]),
            Service(
                id="corp.db",
                provider="corp",
                name="db",
                origins=[Origin("db.corp.example")],
                allowed_ips=["0.0.0.0/0"],
            ),
        ],
        capabilities=[Capability(id="corp.data:read", provider="corp", name="read", level=Privilege.READ)],
        operations=[
            Operation(
                id="corp.api.get",
                service="corp.api",
                selector=RestSelector("GET", "/v1/items"),
                privilege=Privilege.READ,
                requires=[["corp.data:read"]],
            ),
            Operation(
                id="corp.db.get",
                service="corp.db",
                selector=RestSelector("GET", "/v1/rows"),
                privilege=Privilege.READ,
                requires=[["corp.data:read"]],
            ),
        ],
    )
    graph = load_graph(extra_documents=[doc])
    result = compile_(graph, [Need(id="n1", target=Target(operation="corp.api.get"), rationale="r")])
    assert not any(r.endpoint.allowed_ips for r in result.access.rules)
    assert any(r.endpoint.allowed_ips for r in result.boundary.rules)
    found = [d for d in result.access.diagnostics if d.code == "broad-allowed-ips"]
    assert found and "boundary" in found[0].message


# ---------------------------------------------------------------- I3: MCP on an opaque host


def test_mcp_toolbox_on_an_opaque_host_is_connection_level(acme_graph):
    result = compile_(acme_graph, [mcp("n1", "vault.acme.example", "/mcp", tools=("read_secret",))])
    rules = allow_rules(result.access, "vault.acme.example")
    assert len(rules) == 1
    assert rules[0].matcher is None and rules[0].endpoint.protocol == Protocol.L4
    assert rules[0].endpoint.inspection == Inspection.OPAQUE
    assert ("n1", "opaque") in [(w.need, w.reason) for w in result.access.widenings]


def test_mcp_toolbox_on_an_opaque_host_is_removed_under_a_floor(acme_graph):
    result = compile_(acme_graph, [mcp("n1", "vault.acme.example", "/mcp", tools=("read_secret",))], [forbid("acme", Privilege.WRITE)])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_endpoint_level_need_on_an_opaque_host_is_connection_level(acme_graph):
    result = compile_(acme_graph, [raw("n1", "vault.acme.example", None, None)])
    rules = allow_rules(result.access)
    assert len(rules) == 1 and rules[0].matcher is None and rules[0].endpoint.protocol == Protocol.L4


# ---------------------------------------------------------------- I4: model needs


def test_model_need_that_loses_every_grant_to_inference_local_is_unresolved(graph):
    need = raw("n1", "api.openai.com", "POST", "/v1/responses", evidence=LLM_EVIDENCE)
    result = compile_(graph, [need], options=ResolveOptions(route_inference_local=True))
    assert allow_rules(result.access) == []
    assert need_result(result).status == "unresolved"
    assert "no inference.local equivalent" in notes(result)


def test_model_need_that_keeps_some_grants_keeps_its_status(graph):
    need = Need(id="n1", target=Target(concept="llm.inference", provider="openai"), rationale="model")
    result = compile_(graph, [need], options=ResolveOptions(route_inference_local=True))
    assert allow_rules(result.access, "inference.local")
    assert need_result(result).status == "resolved"
    assert "no inference.local equivalent" in notes(result)


def test_llm_config_need_is_always_justified_under_strict_use_case(graph):
    needs = [
        raw("n1", "vllm.example.com", "POST", "/v1/chat/completions", evidence=LLM_EVIDENCE),
        raw("n2", "api.example.com", "POST", "/v1/things"),
    ]
    result = compile_(
        graph,
        needs,
        options=ResolveOptions(strict_use_case=True),
        index=SemanticIndex(graph),
        use_case="Summarize text for the reader.",
    )
    assert allow_rules(result.access, "vllm.example.com")
    n1 = need_result(result)
    assert n1.status == "uncatalogued"
    assert "llm_config" in n1.justified_by
    assert not n1.unjustified
    assert need_result(result, "n2").status == "excluded"


def test_uncatalogued_model_endpoint_suggests_inference_local(graph):
    need = raw("n1", "vllm.example.com", "POST", "/v1/chat/completions", evidence=LLM_EVIDENCE)
    result = compile_(graph, [need])
    assert need_result(result).status == "uncatalogued"
    assert "--route-inference-local" in notes(result)


def test_uncatalogued_tool_endpoint_does_not_suggest_inference_local(graph):
    result = compile_(graph, [raw("n1", "api.example.com", "POST", "/v1/things")])
    assert "--route-inference-local" not in notes(result)


# ---------------------------------------------------------------- I5: mTLS


MTLS = Credential("mtls", reference="key_file, cert_file")


def test_mtls_need_is_connection_level_and_opaque(graph):
    result = compile_(graph, [raw("n1", "api.example.com", "POST", "/v1/things", credential=MTLS)])
    rules = allow_rules(result.access)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.matcher is None and rule.endpoint.protocol == Protocol.L4
    assert rule.endpoint.inspection == Inspection.OPAQUE
    assert ("n1", "mtls", "mtls: OpenShell TLS termination would break client certificates") in [
        (w.need, w.reason, w.detail) for w in result.access.widenings
    ]
    endpoint = next(iter(lower(result.access)["network_policies"].values()))["endpoints"][0]
    assert endpoint.get("tls") == "skip" and "rules" not in endpoint


def test_mtls_mcp_need_is_connection_level(graph):
    need = Need(
        id="n1",
        target=Target(raw=RawEndpoint(host="mcp.example.com", port=443, protocol=Protocol.MCP, path="/mcp", mcp_tools=("x",))),
        rationale="mcp",
        credential=MTLS,
    )
    result = compile_(graph, [need])
    rules = allow_rules(result.access)
    assert len(rules) == 1 and rules[0].matcher is None


def test_mtls_need_is_removed_under_a_provider_floor(graph):
    need = raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell", credential=MTLS)
    result = compile_(graph, [need], [forbid("github", Privilege.WRITE)])
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_mtls_catalogued_need_is_connection_level(graph):
    need = Need(id="n1", target=Target(operation="github.repos.get"), rationale="r", credential=MTLS)
    result = compile_(graph, [need])
    rules = allow_rules(result.access)
    assert rules and all(r.matcher is None and r.endpoint.inspection == Inspection.OPAQUE for r in rules)


# ---------------------------------------------------------------- minor: host normalization


def test_raw_need_host_is_normalized(graph):
    result = compile_(graph, [raw("n1", "API.GitHub.com.", "GET", "/repos/NVIDIA/OpenShell/traffic/{kind}")])
    assert {r.endpoint.host for r in allow_rules(result.access)} == {"api.github.com"}


def test_trailing_dot_host_does_not_bypass_a_provider_floor(graph):
    for path in ("/repos/NVIDIA/OpenShell/issues", "/repos/NVIDIA/OpenShell/uncatalogued"):
        need = raw("n1", "api.github.com.", "POST", path)
        result = compile_(graph, [need], [forbid("github", Privilege.WRITE)])
        assert allow_rules(result.access) == [], path


def test_raw_prohibition_host_is_normalized(graph):
    prohibition = Prohibition(id="p", rationale="never", target=Target(raw=RawEndpoint(host="Evil.Example.com.", port=443)))
    result = compile_(graph, [raw("n1", "evil.example.com", "GET", "/x")], [prohibition])
    assert allow_rules(result.access) == []


# ---------------------------------------------------------------- minor: justification ran


def test_resolution_reports_whether_justification_ran(graph):
    need = raw("n1", "api.example.com", "POST", "/v1/things")
    without_index = compile_(graph, [need], use_case="Post things.")
    assert without_index.justified is False
    assert "justif" not in notes(without_index).lower()
    without_text = compile_(graph, [need], index=SemanticIndex(graph))
    assert without_text.justified is False
    with_both = compile_(graph, [need], index=SemanticIndex(graph), use_case="Read the code of NVIDIA/OpenShell.")
    assert with_both.justified is True
    assert "Not justified by the use case" in notes(with_both)


def test_glob_matching_helper_sanity():
    # The SSE endpoint path covers the server's message path and the stream path.
    assert paths.glob_matches("/v1/**", "/v1/sse")
    assert paths.glob_matches("/v1/**", "/v1/messages")
