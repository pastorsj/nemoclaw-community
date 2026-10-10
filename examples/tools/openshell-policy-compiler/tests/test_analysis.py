# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from policy_compiler.analysis import breadth, permitted_operations, rules_dropped_by_lowering
from policy_compiler.backends.openshell import lift, load_policy_file, lower
from policy_compiler.graph.store import load_graph
from policy_compiler.ir.access import AccessRule, AccessSet, ClientSet, Effect, EndpointKey, Matcher
from policy_compiler.ir.common import Inspection, Protocol

REPO_EXAMPLES = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def graph():
    return load_graph()


def lifted(text_or_path):
    if isinstance(text_or_path, Path):
        return lift(load_policy_file(text_or_path), source=str(text_or_path))
    import yaml

    return lift(yaml.safe_load(text_or_path), source="inline")


def test_connection_level_github_reaches_admin_operations(graph):
    access = lifted(
        "version: 1\nnetwork_policies:\n  g:\n    endpoints: [{host: api.github.com, port: 443, tls: skip}]\n"
        "    binaries: [{path: /usr/bin/python3}]\n"
    )
    permitted = permitted_operations(access, graph)
    assert "github.repos.delete" in permitted
    b = breadth(access, graph, permitted)
    assert b.permitted["admin"] >= 1 and b.connection_level_endpoints == 1


def test_scoped_read_policy_reaches_only_reads(graph):
    access = lifted(REPO_EXAMPLES / "recipes" / "nvidia" / "pr-test-case-assistant" / "policies" / "github-api.yaml")
    b = breadth(access, graph)
    assert b.permitted["write"] == 0 and b.permitted["admin"] == 0
    assert b.permitted["read"] > 0


def test_deny_rule_removes_fully_covered_operation(graph):
    access = lifted(
        "version: 1\nnetwork_policies:\n  g:\n    endpoints:\n"
        "      - host: api.github.com\n        port: 443\n        protocol: rest\n        enforcement: enforce\n"
        "        rules: [{allow: {method: '*', path: '/repos/**'}}]\n"
        "        deny_rules: [{method: DELETE, path: '/repos/*/*'}]\n"
        "    binaries: [{path: /usr/bin/python3}]\n"
    )
    permitted = permitted_operations(access, graph)
    assert "github.repos.delete" not in permitted
    assert "github.repos.put-content" in permitted


def test_audit_mode_does_not_bind_l7_rules(graph):
    access = lifted(
        "version: 1\nnetwork_policies:\n  g:\n    endpoints:\n"
        "      - {host: api.github.com, port: 443, protocol: rest, rules: [{allow: {method: GET, path: /zen}}]}\n"
        "    binaries: [{path: /usr/bin/python3}]\n"
    )
    b = breadth(access, graph)
    assert b.unenforced_endpoints == 1
    assert b.permitted["admin"] >= 1  # audit lets every request through


def test_uncatalogued_preset_adds_score(graph):
    narrow = lifted(
        "version: 1\nnetwork_policies:\n  g:\n    endpoints:\n"
        "      - {host: api.unknown.example, port: 443, protocol: rest, enforcement: enforce, "
        "rules: [{allow: {method: GET, path: /v1/x}}]}\n    binaries: [{path: /usr/bin/python3}]\n"
    )
    broad = lifted(
        "version: 1\nnetwork_policies:\n  g:\n    endpoints:\n"
        "      - {host: api.unknown.example, port: 443, protocol: rest, enforcement: enforce, access: full}\n"
        "    binaries: [{path: /usr/bin/python3}]\n"
    )
    assert breadth(narrow, graph).score < breadth(broad, graph).score


def _endpoint(port, rules, deny=None):
    endpoint = {"host": "api.github.com", "port": port, "protocol": "rest", "enforcement": "enforce", "rules": rules}
    if deny:
        endpoint["deny_rules"] = deny
    return endpoint


def test_deny_in_another_policy_entry_does_not_hide_an_operation(graph):
    from policy_compiler.backends.openshell import lift

    policy = {
        "version": 1,
        "network_policies": {
            "other_binary": {
                "name": "other_binary",
                "binaries": [{"path": "/usr/bin/curl"}],
                "endpoints": [
                    _endpoint(
                        443,
                        [{"allow": {"method": "GET", "path": "/repos/**"}}],
                        [{"method": "PUT", "path": "/repos/*/*/contents/**"}],
                    )
                ],
            },
            "python": {
                "name": "python",
                "binaries": [{"path": "/usr/bin/python3"}],
                "endpoints": [_endpoint(443, [{"allow": {"method": "*", "path": "/repos/**"}}])],
            },
        },
    }
    assert "github.repos.put-content" in permitted_operations(lift(policy, source="p.yaml"), graph)


def test_deny_on_another_port_does_not_hide_an_operation(graph):
    from policy_compiler.backends.openshell import lift

    policy = {
        "version": 1,
        "network_policies": {
            "python": {
                "name": "python",
                "binaries": [{"path": "/usr/bin/python3"}],
                "endpoints": [
                    _endpoint(443, [{"allow": {"method": "*", "path": "/repos/**"}}]),
                    _endpoint(
                        8443,
                        [{"allow": {"method": "GET", "path": "/x"}}],
                        [{"method": "PUT", "path": "/repos/*/*/contents/**"}],
                    ),
                ],
            },
        },
    }
    assert "github.repos.put-content" in permitted_operations(lift(policy, source="p.yaml"), graph)


def test_deny_in_the_same_entry_still_removes_the_operation(graph):
    from policy_compiler.backends.openshell import lift

    policy = {
        "version": 1,
        "network_policies": {
            "python": {
                "name": "python",
                "binaries": [{"path": "/usr/bin/python3"}],
                "endpoints": [
                    _endpoint(
                        443,
                        [{"allow": {"method": "*", "path": "/repos/**"}}],
                        [{"method": "PUT", "path": "/repos/*/*/contents/**"}],
                    )
                ],
            },
        },
    }
    assert "github.repos.put-content" not in permitted_operations(lift(policy, source="p.yaml"), graph)


# --- endpoint route selectors and MCP endpoints (final review C2) ---------------------------------


def _policy(*endpoints):
    return {
        "version": 1,
        "network_policies": {
            "p": {"name": "p", "binaries": [{"path": "/usr/bin/python3"}], "endpoints": list(endpoints)},
        },
    }


def _mcp_endpoint(path=None):
    endpoint = {
        "host": "gitlab.com",
        "port": 443,
        "protocol": "mcp",
        "enforcement": "enforce",
        "rules": [{"allow": {"method": "tools/call", "tool": "get_issue"}}],
    }
    if path is not None:
        endpoint["path"] = path
    return endpoint


def test_mcp_endpoint_without_path_reaches_every_post_and_get_on_the_host(graph):
    # OpenShell reads MCP messages from request bodies, so a POST or GET to any path on the host can carry one.
    access = lift(_policy(_mcp_endpoint()), source="p.yaml")
    permitted = permitted_operations(access, graph)
    assert "gitlab.issues.create" in permitted  # POST, write, on the REST service of the same host
    assert "gitlab.projects.get" in permitted  # GET
    assert "gitlab.git.upload-pack" in permitted  # POST on another service of the same host
    assert breadth(access, graph, permitted).permitted["write"] >= 1


def test_mcp_endpoint_with_path_reaches_only_requests_under_that_path(graph):
    narrow = permitted_operations(lift(_policy(_mcp_endpoint("/api/v4/mcp")), source="p.yaml"), graph)
    assert not {"gitlab.issues.create", "gitlab.projects.get", "gitlab.git.upload-pack"} & set(narrow)
    wide = permitted_operations(lift(_policy(_mcp_endpoint("/api/v4/**")), source="p.yaml"), graph)
    assert "gitlab.issues.create" in wide and "gitlab.projects.get" in wide
    assert "gitlab.git.upload-pack" not in wide


def test_rest_endpoint_path_selector_narrows_what_the_endpoint_reaches(graph):
    endpoint = _endpoint(443, [{"allow": {"method": "*", "path": "/**"}}])
    endpoint["path"] = "/repos/**"
    permitted = permitted_operations(lift(_policy(endpoint), source="p.yaml"), graph)
    assert "github.repos.delete" in permitted
    assert "github.search.code" not in permitted
    assert "github.markdown.render" not in permitted


def test_endpoint_path_does_not_narrow_an_uninspected_endpoint(graph):
    endpoint = {"host": "api.github.com", "port": 443, "path": "/repos/**"}
    assert "github.search.code" in permitted_operations(lift(_policy(endpoint), source="p.yaml"), graph)


def test_deny_on_a_path_scoped_endpoint_covers_only_requests_under_its_path(graph):
    deny_endpoint = _endpoint(
        443, [{"allow": {"method": "GET", "path": "/repos/x/y"}}], [{"method": "DELETE", "path": "/repos/*/*"}]
    )
    deny_endpoint["path"] = "/repos/x/**"
    policy = _policy(_endpoint(443, [{"allow": {"method": "*", "path": "/repos/**"}}]), deny_endpoint)
    assert "github.repos.delete" in permitted_operations(lift(policy, source="p.yaml"), graph)


def test_deny_with_a_query_does_not_cover_the_whole_operation(graph):
    policy = _policy(
        _endpoint(
            443,
            [{"allow": {"method": "*", "path": "/repos/**"}}],
            [{"method": "DELETE", "path": "/repos/*/*", "query": {"force": "true"}}],
        )
    )
    assert "github.repos.delete" in permitted_operations(lift(policy, source="p.yaml"), graph)


def test_deny_on_an_mcp_endpoint_does_not_cover_rest_operations(graph):
    endpoint = _mcp_endpoint()
    endpoint["deny_rules"] = [{"method": "POST"}]
    assert "gitlab.issues.create" in permitted_operations(lift(_policy(endpoint), source="p.yaml"), graph)


# --- rules dropped by lowering (final review I2) ------------------------------------------------------------


def _rule(host, *, matcher=None, protocol=Protocol.REST, inspection=Inspection.L7, path="", effect=Effect.ALLOW):
    return AccessRule(
        effect=effect,
        group=host.replace(".", "_"),
        clients=ClientSet.of(["/usr/bin/python3"]),
        endpoint=EndpointKey(host=host, ports=(443,), protocol=protocol, inspection=inspection, path=path),
        matcher=matcher,
        reasons=["need-01"],
    )


def test_rules_dropped_by_lowering_names_only_what_the_emitted_policy_lacks():
    access = AccessSet(
        rules=[
            _rule("a.example", matcher=Matcher(method="GET", path="/x"), path="/x/**"),  # kept
            _rule("b.example", matcher=Matcher(method="GET", path="/y"), protocol=Protocol.L4),  # dropped
            _rule("c.example", matcher=Matcher(method="GET", path="/z"), inspection=Inspection.OPAQUE),
            _rule("c.example", inspection=Inspection.OPAQUE),  # grants c.example whole, so /z stays reachable
            _rule("d.example", matcher=Matcher(method="tools/call", tool="t"), protocol=Protocol.MCP, path="/mcp"),
        ]
    )
    emitted = lift(lower(access), source="policy.yaml")
    assert rules_dropped_by_lowering(access, emitted) == {1}
