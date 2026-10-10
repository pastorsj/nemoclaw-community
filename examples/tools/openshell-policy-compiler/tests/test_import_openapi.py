# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from policy_compiler.graph.importers.openapi import import_openapi, load_spec_file, slug
from policy_compiler.graph.importers.overlay import apply_overlay, parse_jsonpath
from policy_compiler.graph.model import Origin, RestSelector
from policy_compiler.ir.common import EvidenceKind, Inspection, Layer, Privilege

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def _ops(doc):
    return {op.id: op for op in doc.operations}


def _caps(doc):
    return {cap.id: cap for cap in doc.capabilities}


def _codes(diagnostics):
    return [d.code for d in diagnostics]


# --- Overlay -------------------------------------------------------------------


def _overlay(*actions):
    return {"overlay": "1.0.0", "info": {"title": "t", "version": "1"}, "actions": list(actions)}


SPEC = {
    "info": {"title": "Demo", "contact": {"name": "Team", "email": "a@example.com"}},
    "tags": [{"name": "a"}, {"name": "b"}],
    "paths": {
        "/x": {
            "get": {
                "operationId": "getX",
                "parameters": [
                    {"name": "dummy", "in": "query"},
                    {"name": "keep", "in": "query"},
                    {"name": "dummy", "in": "header"},
                ],
            },
            "post": {"operationId": "postX", "tags": ["one"]},
        },
        "/y": {"get": {"operationId": "getY", "parameters": [{"name": "dummy", "in": "query"}]}},
    },
}


def test_overlay_returns_new_dict_and_leaves_inputs_untouched():
    spec = copy.deepcopy(SPEC)
    overlay = _overlay({"target": "$.info", "update": {"contact": {"name": "Other"}, "x-new": [1]}})
    before_spec, before_overlay = copy.deepcopy(spec), copy.deepcopy(overlay)
    result = apply_overlay(spec, overlay)
    assert spec == before_spec
    assert overlay == before_overlay
    assert result is not spec
    # Mutating the result must not reach the overlay's update value.
    result["info"]["x-new"].append(2)
    assert overlay["actions"][0]["update"]["x-new"] == [1]


def test_overlay_treats_yaml_anchors_as_separate_nodes():
    shared = {"tags": ["a"]}  # what a YAML anchor and alias load as
    spec = {"paths": {"/a": {"get": shared}, "/b": {"get": shared}}}
    one = apply_overlay(spec, _overlay({"target": "$.paths['/a'].get", "update": {"tags": ["b"]}}))
    assert one["paths"]["/a"]["get"]["tags"] == ["a", "b"]
    assert one["paths"]["/b"]["get"]["tags"] == ["a"]
    every = apply_overlay(spec, _overlay({"target": "$.paths.*.get", "update": {"tags": ["c"]}}))
    assert every["paths"]["/a"]["get"]["tags"] == ["a", "c"]
    assert every["paths"]["/b"]["get"]["tags"] == ["a", "c"]
    assert shared == {"tags": ["a"]}


def test_overlay_update_merges_objects_recursively():
    overlay = _overlay(
        {"target": "$.info", "update": {"title": "Renamed", "contact": {"name": "Other"}, "x-extra": {"k": 1}}}
    )
    info = apply_overlay(SPEC, overlay)["info"]
    # A primitive replaces a primitive, nested objects merge, new properties are added.
    assert info["title"] == "Renamed"
    assert info["contact"] == {"name": "Other", "email": "a@example.com"}
    assert info["x-extra"] == {"k": 1}


def test_overlay_update_concatenates_nested_arrays():
    overlay = _overlay({"target": "$.paths['/x'].post", "update": {"tags": ["two"]}})
    assert apply_overlay(SPEC, overlay)["paths"]["/x"]["post"]["tags"] == ["one", "two"]


def test_overlay_update_on_array_target_appends_one_entry():
    overlay = _overlay(
        {"target": "$.tags", "update": {"name": "c"}},
        {"target": "$.paths.*.get.parameters", "update": {"name": "newParam", "in": "query"}},
    )
    result = apply_overlay(SPEC, overlay)
    assert result["tags"] == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert result["paths"]["/x"]["get"]["parameters"][-1] == {"name": "newParam", "in": "query"}
    assert result["paths"]["/y"]["get"]["parameters"][-1] == {"name": "newParam", "in": "query"}


def test_overlay_remove_object_property_and_ignore_update():
    overlay = _overlay({"target": "$.paths['/x'].post", "remove": True, "update": {"summary": "ignored"}})
    result = apply_overlay(SPEC, overlay)
    assert "post" not in result["paths"]["/x"]
    assert "get" in result["paths"]["/x"]


def test_overlay_remove_array_elements_with_filter():
    # The example from the Overlay 1.0.0 specification, with and without parentheses.
    for target in (
        "$.paths.*.get.parameters[?@.name == 'dummy']",
        "$.paths.*.get.parameters[?(@.name == 'dummy')]",
        '$.paths.*.get.parameters[?(@.name == "dummy")]',
    ):
        result = apply_overlay(SPEC, _overlay({"target": target, "remove": True}))
        assert result["paths"]["/x"]["get"]["parameters"] == [{"name": "keep", "in": "query"}]
        assert result["paths"]["/y"]["get"]["parameters"] == []


def test_overlay_filter_on_object_members_and_bracket_names():
    overlay = _overlay(
        {"target": "$.paths.*[?(@.operationId == 'postX')]", "update": {"x-openshell-privilege": "read"}},
        {"target": '$["paths"]["/y"][*]', "update": {"summary": "all y"}},
        {"target": "$['info']", "update": {"x-tagged": True}},
    )
    result = apply_overlay(SPEC, overlay)
    assert result["paths"]["/x"]["post"]["x-openshell-privilege"] == "read"
    assert "x-openshell-privilege" not in result["paths"]["/x"]["get"]
    assert result["paths"]["/y"]["get"]["summary"] == "all y"
    assert result["info"]["x-tagged"] is True


def test_overlay_actions_apply_in_order():
    overlay = _overlay(
        {"target": "$.info", "update": {"x-first": {"a": 1}}},
        {"target": "$.info.x-first", "update": {"b": 2}},
        {"target": "$.info.x-first", "remove": True},
        {"target": "$.info", "update": {"x-first": {"c": 3}}},
    )
    assert apply_overlay(SPEC, overlay)["info"]["x-first"] == {"c": 3}


def test_overlay_no_match_is_a_warning_not_an_exception():
    diagnostics = []
    overlay = _overlay({"target": "$.paths['/missing'].get", "update": {"summary": "x"}})
    result = apply_overlay(SPEC, overlay, diagnostics=diagnostics)
    assert result == SPEC
    assert [(d.level, d.code) for d in diagnostics] == [("warning", "overlay-no-match")]
    assert diagnostics[0].pointer == "actions[0]"
    # Without a diagnostics list the warning is dropped silently.
    assert apply_overlay(SPEC, overlay) == SPEC


def test_overlay_primitive_target_is_a_warning():
    diagnostics = []
    overlay = _overlay({"target": "$.info.title", "update": "New"})
    assert apply_overlay(SPEC, overlay, diagnostics=diagnostics)["info"]["title"] == "Demo"
    assert _codes(diagnostics) == ["overlay-invalid-target"]


@pytest.mark.parametrize("target", ["paths", "$..paths", "$.paths[", "$.paths[?(@.a > 1)]", "$.paths['x"])
def test_overlay_rejects_unsupported_jsonpath(target):
    with pytest.raises(ValueError):
        parse_jsonpath(target)


def test_overlay_rejects_malformed_documents():
    with pytest.raises(ValueError):
        apply_overlay(SPEC, {"overlay": "1.0.0"})
    with pytest.raises(ValueError):
        apply_overlay(SPEC, _overlay({"update": {}}))


def test_overlay_requires_version_and_info():
    actions = [{"target": "$.info", "update": {"x": 1}}]
    with pytest.raises(ValueError, match="overlay"):
        apply_overlay(SPEC, {"info": {"title": "t", "version": "1"}, "actions": actions})
    with pytest.raises(ValueError, match="overlay"):
        apply_overlay(SPEC, {"overlay": 1.0, "info": {"title": "t", "version": "1"}, "actions": actions})
    with pytest.raises(ValueError, match="info"):
        apply_overlay(SPEC, {"overlay": "1.0.0", "actions": actions})


def test_overlay_remove_on_primitive_target_is_a_warning():
    diagnostics = []
    result = apply_overlay(SPEC, _overlay({"target": "$.info.title", "remove": True}), diagnostics=diagnostics)
    assert result["info"]["title"] == "Demo"
    assert [(d.level, d.code) for d in diagnostics] == [("warning", "overlay-invalid-target")]


def test_overlay_unsupported_action_key_is_a_warning_and_skips_the_action():
    diagnostics = []
    overlay = _overlay(
        {"target": "$.info", "copy": "$.paths['/x'].get", "update": {"title": "ignored"}},
        {"target": "$.info", "description": "kept", "x-note": "extensions are allowed", "update": {"x-ok": True}},
    )
    result = apply_overlay(SPEC, overlay, diagnostics=diagnostics)
    assert result["info"]["title"] == "Demo"
    assert result["info"]["x-ok"] is True
    (warning,) = diagnostics
    assert (warning.level, warning.code, warning.pointer) == ("warning", "overlay-unsupported-action", "actions[0]")
    assert "copy" in warning.message


# --- OpenAPI: OAuth scopes --------------------------------------------------------


@pytest.fixture(scope="module")
def scoped():
    spec = load_spec_file(FIXTURES / "scoped.yaml")
    return import_openapi(spec, provider_id="taskco", source="openapi:scoped.yaml", layer=Layer.AGENT)


def test_scoped_provider_and_service(scoped):
    doc, _ = scoped
    assert doc.source == "openapi:scoped.yaml"
    assert [(p.id, p.name, p.layer) for p in doc.providers] == [("taskco", "TaskCo Tasks API", Layer.AGENT)]
    (service,) = doc.services
    assert service.id == "taskco.us-taskco-example"
    assert service.provider == "taskco"
    # Server variables use their defaults.
    assert service.origins == [Origin(host="us.taskco.example", port=8443, scheme="https")]
    assert service.inspection is Inspection.L7
    assert service.allowed_ips == []
    assert service.evidence[0].kind is EvidenceKind.DOCUMENTED
    assert service.evidence[0].pointer == "servers[0]"


def test_scoped_and_or_requirements(scoped):
    doc, _ = scoped
    ops = _ops(doc)
    assert ops["taskco.listtasks"].selector == RestSelector("GET", "/v2/tasks")
    # Root-level security applies when the operation has none.
    assert ops["taskco.listtasks"].requires == [["taskco.tasks:read"]]
    assert ops["taskco.listtasks"].privilege is Privilege.READ
    # Two scopes in one requirement object are an AND: the maximum level counts.
    assert ops["taskco.tasks-create"].requires == [["taskco.tasks:write", "taskco.projects.view"]]
    assert ops["taskco.tasks-create"].privilege is Privilege.WRITE
    # Two requirement objects are an OR: the minimum over them counts. The API key
    # in the second requirement contributes no capability.
    assert ops["taskco.deletetask"].requires == [["taskco.admin:org"], ["taskco.tasks:write"]]
    assert ops["taskco.deletetask"].privilege is Privilege.WRITE
    # The anonymous alternative ({}) contributes no capability.
    assert ops["taskco.gettask"].requires == [["taskco.tasks:read"]]
    # OpenID Connect scopes count like OAuth 2.0 scopes.
    assert ops["taskco.getme"].requires == [["taskco.profile.read"]]
    assert ops["taskco.getme"].privilege is Privilege.READ


def test_scoped_capability_levels_and_evidence(scoped):
    doc, _ = scoped
    caps = _caps(doc)
    documented = caps["taskco.tasks:write"]
    assert (documented.level, documented.provider, documented.name) == (Privilege.WRITE, "taskco", "tasks:write")
    assert documented.description == "Create and update tasks"
    assert documented.evidence[0].kind is EvidenceKind.DOCUMENTED
    assert documented.evidence[0].pointer == "x-openshell-scope-levels"
    inferred = {cap_id: caps[cap_id] for cap_id in ("taskco.projects.view", "taskco.admin:org", "taskco.profile.read")}
    assert {cap_id: cap.level for cap_id, cap in inferred.items()} == {
        "taskco.projects.view": Privilege.READ,
        "taskco.admin:org": Privilege.ADMIN,
        "taskco.profile.read": Privilege.READ,
    }
    for cap in inferred.values():
        assert cap.evidence[0].kind is EvidenceKind.INFERRED
        assert cap.evidence[0].confidence == 0.6
    assert len(doc.capabilities) == len(caps), "each capability is created once"


def test_scoped_operation_evidence(scoped):
    doc, _ = scoped
    ops = _ops(doc)
    listed = ops["taskco.listtasks"].evidence[0]
    assert (listed.kind, listed.source, listed.pointer) == (
        EvidenceKind.DOCUMENTED,
        "openapi:scoped.yaml",
        "paths./tasks.get",
    )
    # A level inferred from a scope name makes the operation's privilege inferred.
    assert ops["taskco.deletetask"].evidence[0].kind is EvidenceKind.INFERRED
    assert ops["taskco.deletetask"].evidence[0].confidence == 0.6


def test_scoped_falls_back_to_method_and_tag_without_oauth(scoped):
    doc, diagnostics = scoped
    ops, caps = _ops(doc), _caps(doc)
    # security: [] overrides the root security; no scopes remain.
    status = ops["taskco.get-status"]
    assert status.requires == [["taskco.status:read"]]
    assert status.privilege is Privilege.READ
    assert status.evidence[0].kind is EvidenceKind.INFERRED
    assert status.evidence[0].confidence == 0.5
    # An API key alone contributes no capability.
    assert ops["taskco.exporttasks"].requires == [["taskco.bulk-exports:write"]]
    assert caps["taskco.bulk-exports:write"].level is Privilege.WRITE
    assert caps["taskco.bulk-exports:write"].evidence[0].kind is EvidenceKind.INFERRED
    (summary,) = [d for d in diagnostics if d.code == "inferred-privilege"]
    assert summary.level == "info"
    assert "5 of 7" in summary.message


def test_scope_level_heuristic_rounds_up():
    spec = {
        "info": {"title": "S"},
        "servers": [{"url": "https://s.example"}],
        "components": {"securitySchemes": {"o": {"type": "oauth2", "flows": {}}}},
        "paths": {
            f"/{index}": {"get": {"operationId": f"op{index}", "security": [{"o": [scope]}]}}
            for index, scope in enumerate(
                [
                    "files.readwrite",
                    "spreadsheets",
                    "drive.readonly",
                    "read_write",
                    "admin.read",
                    "delete_repo",
                    "orders:view",
                    "administration:write",
                    "administrator",
                    "repo:management",
                    "deletion",
                ]
            )
        },
    }
    doc, _ = import_openapi(spec, provider_id="s", source="t", layer=Layer.ORG)
    assert {cap.name: cap.level for cap in doc.capabilities} == {
        "files.readwrite": Privilege.WRITE,
        "spreadsheets": Privilege.WRITE,
        "drive.readonly": Privilege.READ,
        "read_write": Privilege.WRITE,
        "admin.read": Privilege.ADMIN,
        "delete_repo": Privilege.ADMIN,
        "orders:view": Privilege.READ,
        # Admin words match anywhere in the name, so longer forms never understate the level.
        "administration:write": Privilege.ADMIN,
        "administrator": Privilege.ADMIN,
        "repo:management": Privilege.ADMIN,
        "deletion": Privilege.ADMIN,
    }


# --- OpenAPI: no scopes, tags ----------------------------------------------------


@pytest.fixture(scope="module")
def tagged():
    spec = load_spec_file(FIXTURES / "tagged.yaml")
    return import_openapi(spec, provider_id="inv", source="openapi:tagged.yaml", layer=Layer.ORG)


def test_tagged_service_uses_http_default_port(tagged):
    doc, _ = tagged
    (service,) = doc.services
    assert service.id == "inv.inventory-example-com"
    assert service.origins == [Origin(host="inventory.example.com", port=80, scheme="http")]
    assert doc.providers[0].name == "Inventory Service"


def test_tagged_ids_paths_and_capabilities(tagged):
    doc, _ = tagged
    ops = _ops(doc)
    assert list(ops) == [
        "inv.get-items",
        "inv.createitem",
        "inv.put-items-item-id",
        "inv.delete-items-item-id",
        "inv.getblob",
        "inv.getraw",
        "inv.getdochistory",
    ]
    # A trailing slash on the server URL does not double the slash.
    assert ops["inv.get-items"].selector == RestSelector("GET", "/items")
    assert ops["inv.get-items"].requires == [["inv.items:read"]]
    assert ops["inv.get-items"].privilege is Privilege.READ
    assert ops["inv.get-items"].summary == "List items"
    assert ops["inv.get-items"].tags == ["Items"]
    assert ops["inv.createitem"].requires == [["inv.items:write"]]
    assert ops["inv.createitem"].summary == "Create an item."
    assert ops["inv.createitem"].privilege is Privilege.WRITE
    # No tags: the capability falls back to "api".
    assert ops["inv.delete-items-item-id"].requires == [["inv.api:write"]]
    caps = _caps(doc)
    assert caps["inv.items:read"].name == "Items (read)"
    assert all(cap.evidence[0].kind is EvidenceKind.INFERRED for cap in doc.capabilities)
    assert all(op.evidence[0].kind is EvidenceKind.INFERRED for op in doc.operations)
    assert all(op.layer is Layer.ORG for op in doc.operations)


def test_tagged_multi_segment_parameters(tagged):
    doc, _ = tagged
    ops = _ops(doc)
    assert ops["inv.getblob"].multi_segment == ["blob_path"]  # x-multi-segment through $ref
    assert ops["inv.getraw"].multi_segment == ["key"]  # allowReserved on a path-level parameter
    assert ops["inv.getdochistory"].multi_segment == ["doc_path"]  # x-openshell-multi-segment
    assert ops["inv.put-items-item-id"].multi_segment == []


def test_operation_id_slugs():
    assert slug("listOrders") == "listorders"
    assert slug("Tasks/Create") == "tasks-create"
    assert slug("repos/get-content") == "repos-get-content"
    assert slug("  Get Order  By ID!! ") == "get-order-by-id"
    assert slug("orders.internal.example") == "orders-internal-example"


def test_duplicate_operation_ids_are_suffixed():
    spec = {
        "info": {"title": "D"},
        "servers": [{"url": "https://d.example"}],
        "paths": {"/a": {"get": {"operationId": "same"}}, "/b": {"get": {"operationId": "Same"}}},
    }
    doc, diagnostics = import_openapi(spec, provider_id="d", source="t", layer=Layer.ORG)
    assert [op.id for op in doc.operations] == ["d.same", "d.same-2"]
    assert "duplicate-operation-id" in _codes(diagnostics)


# --- OpenAPI: overlay on an internal API -------------------------------------------


@pytest.fixture(scope="module")
def orders():
    spec = load_spec_file(FIXTURES / "orders.yaml")
    overlay = load_spec_file(FIXTURES / "orders-overlay.yaml")
    return import_openapi(spec, provider_id="acme", source="openapi:orders.yaml", layer=Layer.ORG, overlay=overlay)


def test_orders_overlay_sets_allowed_ips(orders):
    doc, _ = orders
    (service,) = doc.services
    assert service.id == "acme.orders-internal-example"
    assert service.origins == [Origin(host="orders.internal.example")]
    assert service.allowed_ips == ["10.20.0.0/16"]
    assert doc.providers[0].name == "ACME Orders"


def test_orders_overlay_marks_search_as_read(orders):
    doc, _ = orders
    ops = _ops(doc)
    search = ops["acme.searchorders"]
    assert search.selector == RestSelector("POST", "/api/orders/search")
    assert search.privilege is Privilege.READ
    assert search.requires == [["acme.orders:read"]]
    assert search.evidence[0].kind is EvidenceKind.DOCUMENTED
    assert search.evidence[0].pointer == "paths./orders/search.post"
    # Without annotation, POST is inferred as write.
    assert ops["acme.createorder"].privilege is Privilege.WRITE
    assert ops["acme.createorder"].evidence[0].kind is EvidenceKind.INFERRED
    # x-openshell-privilege wins over the method guess, and names the capability level.
    assert ops["acme.cancelorder"].privilege is Privilege.ADMIN
    assert ops["acme.cancelorder"].requires == [["acme.orders:admin"]]
    assert _caps(doc)["acme.orders:admin"].level is Privilege.ADMIN


def test_orders_overlay_concept_links(orders):
    doc, _ = orders
    links = {link.concept: link for link in doc.concept_links}
    assert links["orders.read"].realized_by == ["acme.orders:read"]
    assert links["orders.write"].realized_by == ["acme.orders:write"]
    assert links["orders.read"].layer is Layer.ORG
    assert links["orders.read"].evidence[0].kind is EvidenceKind.DOCUMENTED


def test_orders_overlay_diagnostics(orders):
    _, diagnostics = orders
    (no_match,) = [d for d in diagnostics if d.code == "overlay-no-match"]
    assert no_match.level == "warning"
    assert "/legacy/orders" in no_match.message
    (summary,) = [d for d in diagnostics if d.code == "inferred-privilege"]
    assert "3 of 5" in summary.message


def test_explicit_capability_extension():
    spec = {
        "info": {"title": "Acme"},
        "servers": [{"url": "https://x.example", "x-openshell-inspection": "opaque"}],
        "x-openshell-allowed-ips": ["10.0.0.0/8"],
        "x-openshell-concepts": ["orders.read"],
        "paths": {
            "/one": {"post": {"operationId": "one", "x-openshell-capability": "acme.orders:read"}},
            "/two": {
                "delete": {
                    "operationId": "two",
                    "x-openshell-capability": ["orders:write", "acme.orders:admin"],
                    "x-openshell-concepts": ["orders.write"],
                }
            },
        },
    }
    doc, _ = import_openapi(spec, provider_id="acme", source="t", layer=Layer.ORG)
    ops = _ops(doc)
    # A string is one capability.
    assert ops["acme.one"].requires == [["acme.orders:read"]]
    assert ops["acme.one"].privilege is Privilege.READ
    assert ops["acme.one"].evidence[0].kind is EvidenceKind.DOCUMENTED
    # A list is an OR of single capabilities; missing provider prefixes are added.
    assert ops["acme.two"].requires == [["acme.orders:write"], ["acme.orders:admin"]]
    assert ops["acme.two"].privilege is Privilege.WRITE
    caps = _caps(doc)
    assert caps["acme.orders:admin"].level is Privilege.ADMIN
    assert caps["acme.orders:read"].evidence[0].kind is EvidenceKind.DOCUMENTED
    (service,) = doc.services
    assert service.inspection is Inspection.OPAQUE
    assert service.allowed_ips == ["10.0.0.0/8"]
    # The root concepts apply where the operation sets none.
    links = {link.concept: link.realized_by for link in doc.concept_links}
    assert links == {"orders.read": ["acme.orders:read"], "orders.write": ["acme.orders:write", "acme.orders:admin"]}


def test_capability_id_level_is_documented_even_when_inferred_first():
    spec = {
        "info": {"title": "Acme"},
        "servers": [{"url": "https://x.example"}],
        "paths": {
            "/a": {"get": {"operationId": "a", "tags": ["orders"]}, "trace": {"operationId": "t"}},
            "/b": {"post": {"operationId": "b", "x-openshell-capability": "orders:read"}},
        },
    }
    doc, _ = import_openapi(spec, provider_id="acme", source="t", layer=Layer.ORG)
    ops = _ops(doc)
    assert ops["acme.a"].evidence[0].kind is EvidenceKind.INFERRED
    assert ops["acme.b"].requires == [["acme.orders:read"]]
    assert ops["acme.b"].privilege is Privilege.READ
    assert ops["acme.b"].evidence[0].kind is EvidenceKind.DOCUMENTED
    # A method with no known level stays unknown and needs an admin-level grant.
    assert ops["acme.t"].privilege is Privilege.UNKNOWN
    assert ops["acme.t"].requires == [["acme.api:admin"]]


def test_server_level_extensions_override_root_and_invalid_values_warn():
    spec = {
        "info": {"title": "Acme"},
        "x-openshell-allowed-ips": ["10.0.0.0/8"],
        "servers": [{"url": "https://x.example", "x-openshell-allowed-ips": ["192.168.1.0/24", "not-an-ip"]}],
        "paths": {"/a": {"get": {"x-openshell-privilege": "superuser"}}},
    }
    doc, diagnostics = import_openapi(spec, provider_id="acme", source="t", layer=Layer.ORG)
    assert doc.services[0].allowed_ips == ["192.168.1.0/24"]
    assert doc.operations[0].privilege is Privilege.READ
    assert _codes(diagnostics).count("invalid-extension") == 2


# --- OpenAPI: server URLs ------------------------------------------------------------


RELATIVE = {
    "info": {"title": "Rel"},
    "servers": [{"url": "/api/v1"}],
    "paths": {"/things/{id}": {"get": {"operationId": "getThing"}}},
}


def test_relative_server_url_without_server_url_is_an_error():
    doc, diagnostics = import_openapi(RELATIVE, provider_id="rel", source="t", layer=Layer.AGENT)
    assert doc.services == []
    assert doc.operations == []
    assert [(d.level, d.code) for d in diagnostics] == [("error", "relative-server-url")]
    assert diagnostics[0].pointer == "servers[0].url"


def test_missing_servers_is_a_relative_server_url():
    spec = {"info": {"title": "None"}, "paths": {"/a": {"get": {}}}}
    _, diagnostics = import_openapi(spec, provider_id="n", source="t", layer=Layer.AGENT)
    assert _codes(diagnostics) == ["relative-server-url"]


def test_relative_server_url_resolves_against_server_url():
    doc, diagnostics = import_openapi(
        RELATIVE,
        provider_id="rel",
        source="t",
        layer=Layer.AGENT,
        server_url="https://things.internal.example:9443",
    )
    assert "relative-server-url" not in _codes(diagnostics)
    (service,) = doc.services
    assert service.id == "rel.things-internal-example"
    assert service.origins == [Origin(host="things.internal.example", port=9443)]
    assert service.evidence[0].kind is EvidenceKind.USER
    assert doc.operations[0].selector == RestSelector("GET", "/api/v1/things/{id}")


def test_server_url_overrides_an_absolute_server():
    spec = dict(RELATIVE, servers=[{"url": "https://prod.example/api/v1"}])
    doc, _ = import_openapi(
        spec, provider_id="rel", source="t", layer=Layer.AGENT, server_url="http://staging.example/base/"
    )
    assert doc.services[0].origins == [Origin(host="staging.example", port=80, scheme="http")]
    assert doc.operations[0].selector == RestSelector("GET", "/base/things/{id}")


@pytest.mark.parametrize("servers", [None, []])
def test_server_url_is_used_as_given_when_the_spec_has_no_servers(servers):
    spec = {"info": {"title": "NoServers"}, "paths": {"/things": {"get": {"operationId": "list"}}}}
    if servers is not None:
        spec["servers"] = servers
    doc, diagnostics = import_openapi(
        spec, provider_id="ns", source="t", layer=Layer.AGENT, server_url="https://api.example.com/v1/"
    )
    assert "relative-server-url" not in _codes(diagnostics)
    assert doc.services[0].origins == [Origin(host="api.example.com")]
    assert doc.services[0].evidence[0].kind is EvidenceKind.USER
    assert doc.operations[0].selector == RestSelector("GET", "/v1/things")


def test_path_and_operation_servers_are_ignored_with_a_warning():
    spec = {
        "info": {"title": "S"},
        "servers": [{"url": "https://root.example"}],
        "paths": {
            "/a": {"servers": [{"url": "https://other.example"}], "get": {"operationId": "a"}},
            "/b": {"get": {"operationId": "b", "servers": [{"url": "https://third.example"}]}},
        },
    }
    doc, diagnostics = import_openapi(spec, provider_id="s", source="t", layer=Layer.ORG)
    assert [op.service for op in doc.operations] == ["s.root-example", "s.root-example"]
    ignored = [d for d in diagnostics if d.code == "ignored-server-override"]
    assert [(d.level, d.pointer) for d in ignored] == [
        ("warning", "paths./a.servers"),
        ("warning", "paths./b.get.servers"),
    ]
    assert "root.example" in ignored[0].message


def test_capability_without_level_gets_the_operation_level():
    spec = {
        "info": {"title": "Acme"},
        "servers": [{"url": "https://x.example"}],
        "paths": {
            "/orders": {
                "get": {"operationId": "list", "x-openshell-capability": "orders"},
                "post": {"operationId": "create", "x-openshell-capability": ["orders", "acme.billing"]},
            },
            "/orders/search": {
                "post": {"operationId": "search", "x-openshell-capability": "orders", "x-openshell-privilege": "read"}
            },
        },
    }
    doc, diagnostics = import_openapi(spec, provider_id="acme", source="t", layer=Layer.ORG)
    ops, caps = _ops(doc), _caps(doc)
    # The level comes from the HTTP method, so it is inferred.
    assert ops["acme.list"].requires == [["acme.orders:read"]]
    assert ops["acme.list"].privilege is Privilege.READ
    assert ops["acme.list"].evidence[0].kind is EvidenceKind.INFERRED
    assert ops["acme.list"].evidence[0].confidence == 0.5
    assert caps["acme.orders:read"].name == "orders:read"
    assert caps["acme.orders:read"].evidence[0].kind is EvidenceKind.INFERRED
    assert ops["acme.create"].requires == [["acme.orders:write"], ["acme.billing:write"]]
    assert ops["acme.create"].privilege is Privilege.WRITE
    # The level comes from x-openshell-privilege, so it is documented.
    assert ops["acme.search"].requires == [["acme.orders:read"]]
    assert ops["acme.search"].evidence[0].kind is EvidenceKind.DOCUMENTED
    (summary,) = [d for d in diagnostics if d.code == "inferred-privilege"]
    assert "2 of 3" in summary.message


def test_malformed_shapes_do_not_crash():
    spec = {
        "info": "not a mapping",
        "servers": {"url": "https://x.example"},
        "components": {"securitySchemes": []},
        "security": "oauth",
        "paths": {"/a": {"parameters": "p", "get": {"tags": "orders", "security": {"o": []}, "parameters": 3}}},
    }
    doc, diagnostics = import_openapi(
        spec, provider_id="m", source="t", layer=Layer.ORG, server_url="https://x.example"
    )
    assert [op.id for op in doc.operations] == ["m.get-a"]
    assert doc.operations[0].requires == [["m.api:read"]]
    assert (
        import_openapi({"paths": []}, provider_id="m", source="t", layer=Layer.ORG, server_url="https://x")[
            0
        ].operations
        == []
    )


def test_swagger_2_is_reported_as_unsupported():
    spec = {"swagger": "2.0", "host": "api.example.com", "paths": {"/a": {"get": {}}}}
    doc, diagnostics = import_openapi(spec, provider_id="old", source="t", layer=Layer.AGENT)
    assert doc.operations == []
    assert [(d.level, d.code) for d in diagnostics] == [("error", "unsupported-openapi-version")]


def test_load_spec_file_reads_json(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"openapi": "3.1.0", "info": {"title": "J"}, "paths": {}}', encoding="utf-8")
    assert load_spec_file(path)["info"]["title"] == "J"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_spec_file(path)
