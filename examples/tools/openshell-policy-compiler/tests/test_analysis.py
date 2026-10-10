# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from policy_compiler.analysis import breadth, permitted_operations
from policy_compiler.backends.openshell import lift, load_policy_file
from policy_compiler.graph.store import load_graph

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
