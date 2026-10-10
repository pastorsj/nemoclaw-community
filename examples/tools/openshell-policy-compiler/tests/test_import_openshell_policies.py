# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from policy_compiler.graph.importers.openshell_policies import glob_to_template, import_openshell_policies
from policy_compiler.graph.store import load_graph
from policy_compiler.ir.common import Layer, Privilege


def policy(host, rules, binaries=("/usr/bin/curl",), **endpoint):
    return {
        "version": 1,
        "network_policies": {
            "p": {
                "name": "p",
                "endpoints": [
                    {"host": host, "port": 443, "protocol": "rest", "enforcement": "enforce", "rules": rules, **endpoint}
                ],
                "binaries": [{"path": b} for b in binaries],
            }
        },
    }


def test_glob_to_template():
    assert glob_to_template("/v1/items/*") == ("/v1/items/{p1}", [])
    assert glob_to_template("/repos/**") == ("/repos/{rest}", ["rest"])
    assert glob_to_template("/bot*/send") == ("/bot{p1}/send", [])


def test_known_host_yields_usage_only():
    known = load_graph()
    doc, usage = import_openshell_policies(
        [("a.yaml", policy("api.github.com", [{"allow": {"method": "GET", "path": "/repos/**"}}]))],
        known=known,
    )
    assert doc.services == [] and doc.operations == []
    assert usage["api.github.com"]["catalogued"] is True
    assert usage["api.github.com"]["methods"] == ["GET"]


def test_unknown_host_becomes_mined_service_and_operations():
    known = load_graph()
    p1 = policy("retail.example.com", [{"allow": {"method": "GET", "path": "/v1/items/*"}}])
    p2 = policy(
        "retail.example.com",
        [{"allow": {"method": "GET", "path": "/v1/items/*"}}, {"allow": {"method": "POST", "path": "/v1/orders"}}],
        binaries=("/usr/local/bin/retail-agent",),
    )
    doc, usage = import_openshell_policies([("a.yaml", p1), ("b.yaml", p2)], known=known, layer=Layer.ORG)
    assert [s.id for s in doc.services] == ["mined.retail-example-com"]
    ops = {(o.selector.method, o.selector.path): o for o in doc.operations}
    assert set(ops) == {("GET", "/v1/items/{p1}"), ("POST", "/v1/orders")}
    assert ops[("GET", "/v1/items/{p1}")].privilege == Privilege.READ
    assert ops[("POST", "/v1/orders")].requires == []
    assert len(ops[("GET", "/v1/items/{p1}")].evidence) == 2  # seen in both policies
    assert usage["retail.example.com"]["policies"] == ["a.yaml", "b.yaml"]
    assert [c.paths for c in doc.clients] == [["/usr/local/bin/retail-agent"]]  # curl is already a base client
    # The mined document loads cleanly into a layered graph and matches the declared call.
    graph = load_graph(extra_documents=[doc])
    matches = graph.match_rest("retail.example.com", 443, "GET", "/v1/items/{id}")
    assert [glob for _, glob in matches] == ["/v1/items/*"]
