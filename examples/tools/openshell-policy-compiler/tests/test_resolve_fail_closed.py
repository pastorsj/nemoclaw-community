# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for resolver paths that granted more than the contract allows.

Each test reproduces a review finding. When the compiler is unsure, it must
grant less and say so.
"""

import pytest

from policy_compiler import paths
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
from policy_compiler.ir.access import Effect
from policy_compiler.ir.common import Privilege, Protocol
from policy_compiler.ir.contract import Contract, Need, Prohibition, RawEndpoint, Target
from policy_compiler.resolve import ResolveOptions, resolve
from policy_compiler.semantic import SemanticIndex

PY = "/usr/bin/python3"
GIT = "/usr/bin/git"
SCOPE = {"owner": "NVIDIA", "repo": "OpenShell"}


@pytest.fixture(scope="module")
def graph():
    return load_graph()


@pytest.fixture(scope="module")
def mcp_graph():
    doc = CatalogDocument(
        providers=[Provider(id="acme", name="Acme")],
        services=[
            Service(
                id="acme.mcp",
                provider="acme",
                name="mcp",
                origins=[Origin("mcp.acme.example")],
                protocol=Protocol.MCP,
            )
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


def raw(nid, host, method, path, prefixes=(), **kwargs):
    return Need(
        id=nid,
        target=Target(raw=RawEndpoint(host=host, port=443, method=method, path=path, **kwargs)),
        rationale=nid,
        path_prefixes=list(prefixes),
    )


def forbid(provider, level):
    return Prohibition(id="p", rationale="no", provider=provider, min_privilege=level)


def allow_rules(access, host=None):
    return [
        r for r in access.rules if r.effect == Effect.ALLOW and (host is None or r.endpoint.host == host)
    ]


def admits(access, method, host, path, client=PY):
    """True when an allow rule admits the request and no deny rule on the same endpoint blocks it."""
    allowed = denied = False
    for r in access.rules:
        if r.endpoint.host != host or client not in r.clients.paths:
            continue
        if r.matcher is None:
            allowed |= r.effect == Effect.ALLOW
            continue
        if r.matcher.path is None or not paths.method_covers(r.matcher.method, method):
            continue
        if paths.glob_matches(r.matcher.path, path):
            if r.effect == Effect.ALLOW:
                allowed = True
            else:
                denied = True
    return allowed and not denied


def need_result(result, need_id="n1"):
    return next(n for n in result.needs if n.need == need_id)


# ---------------------------------------------------------------- C1: provider prohibitions


def test_provider_prohibition_removes_uncatalogued_write(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "DELETE", "/repos/NVIDIA/OpenShell/git/refs/{ref}")],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"
    assert any(d.code == "prohibited" for d in result.access.diagnostics)


def test_provider_prohibition_keeps_uncatalogued_read_below_floor(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/traffic/{kind}")],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    assert admits(result.access, "GET", "api.github.com", "/repos/NVIDIA/OpenShell/traffic/views")


def test_provider_prohibition_narrows_any_method_glob_to_get(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**", prefixes=["/repos/NVIDIA/OpenShell"], path_is_glob=True)],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    rules = allow_rules(result.access, "api.github.com")
    assert rules and all(r.matcher.method == "GET" for r in rules)
    for probe in (
        "/repos/NVIDIA/OpenShell/git/refs/heads/main",
        "/repos/NVIDIA/OpenShell/hooks/1",
        "/repos/NVIDIA/OpenShell/collaborators/evil",
    ):
        assert not admits(result.access, "DELETE", "api.github.com", probe)
    assert admits(result.access, "GET", "api.github.com", "/repos/NVIDIA/OpenShell/contents/README.md")
    operations = {op for r in rules for op in r.operations}
    assert all(graph.operations[op].privilege == Privilege.READ for op in operations)
    assert any("narrowed to GET by prohibition" in w.detail for w in result.access.widenings)
    assert any("narrowed to GET by prohibition" in note for note in need_result(result).notes)


def test_provider_prohibition_at_read_removes_any_method_glob(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**", path_is_glob=True)],
        prohibitions=[forbid("github", Privilege.READ)],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_provider_prohibition_narrows_endpoint_level_need(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", None, None)],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    rules = allow_rules(result.access, "api.github.com")
    assert [(r.matcher.method, r.matcher.path) for r in rules] == [("GET", "/**")]
    assert not admits(result.access, "DELETE", "api.github.com", "/repos/NVIDIA/OpenShell")


def test_provider_prohibition_removes_connection_level_need(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", None, None, protocol=Protocol.TCP)],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    assert need_result(result).status == "conflict"


def test_provider_prohibition_ignores_other_providers_hosts(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "integrate.api.nvidia.com", None, None, protocol=Protocol.TCP)],
        prohibitions=[forbid("github", Privilege.WRITE)],
    )
    result = resolve(contract, graph)
    assert len(allow_rules(result.access, "integrate.api.nvidia.com")) == 1


# ---------------------------------------------------------------- C2 and I7: MCP grants


def test_mcp_tool_grants_carry_only_their_own_operations(mcp_graph):
    need = Need(
        id="n1",
        target=Target(
            raw=RawEndpoint(
                host="mcp.acme.example", port=443, protocol=Protocol.MCP, path="/mcp", mcp_tools=("search", "delete_repo")
            )
        ),
        rationale="mcp",
    )
    result = resolve(Contract(agent="a", default_clients=[PY], needs=[need]), mcp_graph)
    by_tool = {(r.matcher.method, r.matcher.tool): r.operations for r in allow_rules(result.access)}
    assert by_tool[("tools/call", "search")] == ["acme.search"]
    assert by_tool[("tools/call", "delete_repo")] == ["acme.delete_repo"]
    for method in ("initialize", "notifications/initialized", "tools/list"):
        assert by_tool[(method, None)] == []


@pytest.mark.parametrize(
    "prohibition",
    [
        Prohibition(id="p", rationale="no admin", provider="acme", min_privilege=Privilege.ADMIN),
        Prohibition(id="p", rationale="never delete", target=Target(operation="acme.delete_repo")),
    ],
)
def test_mcp_prohibited_tool_is_removed(mcp_graph, prohibition):
    need = Need(
        id="n1",
        target=Target(
            raw=RawEndpoint(
                host="mcp.acme.example", port=443, protocol=Protocol.MCP, path="/mcp", mcp_tools=("search", "delete_repo")
            )
        ),
        rationale="mcp",
    )
    result = resolve(Contract(agent="a", default_clients=[PY], needs=[need], prohibitions=[prohibition]), mcp_graph)
    tools = {(r.matcher.method, r.matcher.tool) for r in allow_rules(result.access)}
    assert ("tools/call", "delete_repo") not in tools
    assert ("tools/call", "search") in tools
    assert ("initialize", None) in tools
    assert need_result(result).status == "conflict"


def test_mcp_capability_need_gets_lifecycle_methods_once(mcp_graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[
            Need(id="n1", target=Target(capability="acme.repo:read"), rationale="x"),
            Need(id="n2", target=Target(operation="acme.delete_repo"), rationale="y"),
        ],
    )
    result = resolve(contract, mcp_graph)
    matchers = sorted((r.matcher.method, r.matcher.tool or "") for r in allow_rules(result.access))
    assert matchers == [
        ("initialize", ""),
        ("notifications/initialized", ""),
        ("tools/call", "delete_repo"),
        ("tools/call", "search"),
        ("tools/list", ""),
    ]


# ---------------------------------------------------------------- C4 and I10: client uses


def test_raw_need_git_operations_are_scoped_to_declared_repository(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY, GIT],
        needs=[raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/{path}")],
    )
    result = resolve(contract, graph)
    git_rules = [r for r in allow_rules(result.access) if GIT in r.clients.paths]
    assert git_rules
    for r in git_rules:
        assert r.endpoint.host == "github.com"
        assert r.matcher.path.startswith("/NVIDIA/OpenShell.git/")
    # git never receives the REST grant itself.
    assert all(GIT not in r.clients.paths for r in allow_rules(result.access, "api.github.com"))


def test_raw_need_without_bound_repository_skips_git_operations(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY, GIT],
        needs=[raw("n1", "api.github.com", "GET", "/repos/{owner}/{repo}/contents/{path}")],
    )
    result = resolve(contract, graph)
    assert all(GIT not in r.clients.paths for r in allow_rules(result.access))
    assert any("implicit operation" in note and "git" in note for note in need_result(result).notes)


def test_raw_need_for_git_only_grants_nothing_but_implicit_uses(graph):
    contract = Contract(
        agent="a",
        default_clients=[GIT],
        needs=[raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/contents/{path}")],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access, "api.github.com") == []
    assert all(r.endpoint.host == "github.com" for r in allow_rules(result.access))


def test_boundary_gives_git_no_rest_grants(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY, GIT],
        needs=[
            Need(
                id="n1",
                target=Target(concept="source-code.read", provider="github", scope=SCOPE),
                rationale="read",
            )
        ],
    )
    result = resolve(contract, graph)
    git_ops = {"github.git.info-refs", "github.git.upload-pack", "github.git.receive-pack"}
    for r in allow_rules(result.boundary):
        if GIT in r.clients.paths:
            assert set(r.operations) <= git_ops, r
        else:
            assert not set(r.operations) & git_ops, r


# ---------------------------------------------------------------- I5: glob branch


def test_glob_branch_drops_path_outside_url_allow_list(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "GET", "/repos/**", prefixes=["/repos/NVIDIA/OpenShell"], path_is_glob=True)],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    assert need_result(result).status == "unresolved"
    assert any("url_allow_list" in note for note in need_result(result).notes)


def test_glob_branch_keeps_path_inside_url_allow_list(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/**", prefixes=["/repos/NVIDIA/OpenShell"], path_is_glob=True)],
    )
    result = resolve(contract, graph)
    assert [(r.matcher.method, r.matcher.path) for r in allow_rules(result.access)] == [
        ("GET", "/repos/NVIDIA/OpenShell/**")
    ]


def test_glob_branch_records_templated_method_widening(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**", path_is_glob=True)],
    )
    result = resolve(contract, graph)
    assert ("n1", "templated-method") in [(w.need, w.reason) for w in result.access.widenings]


# ---------------------------------------------------------------- C5: adjacent placeholders


def test_adjacent_placeholders_stay_in_one_segment(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[raw("n1", "api.example.com", "GET", "/v1/files/{name}{ext}")],
    )
    result = resolve(contract, graph)
    assert [r.matcher.path for r in allow_rules(result.access)] == ["/v1/files/*"]


# ---------------------------------------------------------------- I1: capability and concept prohibitions


def test_capability_prohibition_on_write_keeps_reads(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[Need(id="n1", target=Target(capability="github.contents:read", scope=SCOPE), rationale="read")],
        prohibitions=[Prohibition(id="p", rationale="no writes", target=Target(capability="github.contents:write"))],
    )
    result = resolve(contract, graph)
    assert "github.repos.get-content" not in result.forbidden_operations
    assert "github.repos.put-content" in result.forbidden_operations
    assert need_result(result).status == "resolved"
    assert admits(result.access, "GET", "api.github.com", "/repos/NVIDIA/OpenShell/contents/README.md")


def test_capability_prohibition_on_read_forbids_writes_too(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[Need(id="n1", target=Target(capability="github.issues:read", scope=SCOPE), rationale="read")],
        prohibitions=[Prohibition(id="p", rationale="no code", target=Target(capability="github.contents:read"))],
    )
    result = resolve(contract, graph)
    assert "github.repos.get-content" in result.forbidden_operations
    assert "github.repos.put-content" in result.forbidden_operations
    assert "github.issues.list" not in result.forbidden_operations


def test_concept_prohibition_on_write_keeps_reads(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[
            Need(id="n1", target=Target(concept="source-code.read", provider="github", scope=SCOPE), rationale="read")
        ],
        prohibitions=[Prohibition(id="p", rationale="never push", target=Target(concept="source-code.write"))],
    )
    result = resolve(contract, graph)
    assert "github.repos.get-content" not in result.forbidden_operations
    assert "github.repos.put-content" in result.forbidden_operations
    assert need_result(result).status == "resolved"


# ---------------------------------------------------------------- I2: strict use case


def test_strict_use_case_requires_every_operation_justified(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        use_case="Read the source code of NVIDIA/OpenShell to explain its architecture.",
        needs=[raw("n1", "api.github.com", "*", "/repos/NVIDIA/OpenShell/**", path_is_glob=True)],
    )
    result = resolve(contract, graph, ResolveOptions(strict_use_case=True), concept_index=SemanticIndex(graph))
    assert allow_rules(result.access) == []
    n1 = need_result(result)
    assert n1.status == "excluded"
    assert "github.issues.create" in n1.unjustified


def test_unjustified_operations_are_a_structured_field(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        use_case="Read the source code of NVIDIA/OpenShell to explain its architecture.",
        needs=[raw("n1", "api.github.com", "GET", "/repos/NVIDIA/OpenShell/issues")],
    )
    result = resolve(contract, graph, concept_index=SemanticIndex(graph))
    assert need_result(result).unjustified == ["github.issues.list"]


# ---------------------------------------------------------------- I6: concepts


def test_concept_without_provider_realized_by_several_providers_is_ambiguous(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[Need(id="n1", target=Target(concept="llm.inference"), rationale="model")],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    n1 = need_result(result)
    assert n1.status == "unresolved"
    assert any("ambiguous: name a provider" in note for note in n1.notes)
    assert any(d.level == "warning" and d.pointer == "n1" for d in result.access.diagnostics)


def test_concept_expansion_records_a_widening(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[
            Need(id="n1", target=Target(concept="source-code.read", provider="github", scope=SCOPE), rationale="read")
        ],
    )
    result = resolve(contract, graph)
    assert ("n1", "concept-level") in [(w.need, w.reason) for w in result.access.widenings]


# ---------------------------------------------------------------- I8: scope values


@pytest.mark.parametrize(
    "scope",
    [
        {"owner": "*", "repo": "**"},
        {"owner": "NVIDIA", "repo": "x/../.."},
        {"owner": "NVIDIA", "repo": ".."},
        {"owner": ".", "repo": "OpenShell"},
        {"owner": "NVIDIA", "repo": ""},
    ],
)
def test_invalid_scope_values_leave_the_need_unresolved(graph, scope):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[Need(id="n1", target=Target(capability="github.contents:read", scope=scope), rationale="read")],
    )
    result = resolve(contract, graph)
    assert allow_rules(result.access) == []
    n1 = need_result(result)
    assert n1.status == "unresolved"
    assert any("scope" in note for note in n1.notes)


def test_valid_scope_values_are_accepted(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[
            Need(
                id="n1",
                target=Target(capability="github.contents:read", scope={"owner": "my-org_1", "repo": "a.b%2Dc"}),
                rationale="read",
            )
        ],
    )
    result = resolve(contract, graph)
    assert need_result(result).status == "resolved"


# ---------------------------------------------------------------- report support


def test_forbidden_operations_are_counted_per_prohibition(graph):
    contract = Contract(
        agent="a",
        default_clients=[PY],
        needs=[],
        prohibitions=[
            Prohibition(id="p-write", rationale="no", provider="github", min_privilege=Privilege.WRITE),
            Prohibition(id="p-op", rationale="no", target=Target(operation="github.repos.get")),
        ],
    )
    result = resolve(contract, graph)
    assert result.forbidden_by["p-op"] == ["github.repos.get"]
    assert "github.repos.get" not in result.forbidden_by["p-write"]
    assert "github.repos.put-content" in result.forbidden_by["p-write"]


# ---------------------------------------------------------------- write intent in justification


def justified_concepts(graph, text):
    contract = Contract(agent="a", default_clients=[PY], use_case=text, needs=[])
    return resolve(contract, graph, concept_index=SemanticIndex(graph)).use_case_concepts


def test_negated_write_request_justifies_no_write_concept(graph):
    concepts = justified_concepts(graph, "Read the code. Never change the repository.")
    assert "source-code.read" in concepts
    assert not [c for c in concepts if graph.concepts[c].privilege.at_least(Privilege.WRITE)]


def test_write_request_with_matching_object_justifies_write_concept(graph):
    concepts = justified_concepts(graph, "fix bugs and push commits to NVIDIA/OpenShell on GitHub")
    assert "source-code.write" in concepts


def test_write_verb_without_matching_object_justifies_no_write_concept(graph):
    concepts = justified_concepts(graph, "List open issues of NVIDIA/OpenShell.")
    assert "issues.write" not in concepts


def test_write_verb_acting_on_another_concept_justifies_no_write_concept(graph):
    concepts = justified_concepts(graph, "Open the code of NVIDIA/OpenShell and explain it.")
    assert not [c for c in concepts if graph.concepts[c].privilege.at_least(Privilege.WRITE)]
