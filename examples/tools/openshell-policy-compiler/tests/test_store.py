# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import copy

import pytest
import yaml

from policy_compiler.graph.model import (
    CatalogDocument,
    Concept,
    McpSelector,
    Operation,
    Origin,
    Provider,
    RestSelector,
)
from policy_compiler.graph.store import (
    BASE_CATALOG_DIR,
    CatalogError,
    Graph,
    dump_catalog_document,
    load_catalog_file,
    load_graph,
    parse_catalog_document,
)
from policy_compiler.ir.common import (
    Evidence,
    EvidenceKind,
    Inspection,
    Layer,
    Privilege,
    Protocol,
)

# Every field from docs/catalog-format.md, with non-default values where possible.
FULL_DOC = {
    "catalog_version": 1,
    "providers": [{"id": "acme", "name": "ACME Corp", "description": "Internal services."}],
    "services": [
        {
            "id": "acme.orders",
            "provider": "acme",
            "name": "Orders API",
            "protocol": "rest",
            "inspection": "opaque",
            "origins": [{"host": "orders.internal.example", "port": 8443, "scheme": "https"}],
            "allowed_ips": ["10.20.0.0/16"],
            "description": "Orders.",
        },
        {
            "id": "acme.tools",
            "provider": "acme",
            "name": "Tools",
            "protocol": "mcp",
            "origins": [{"host": "tools.internal.example"}],
        },
    ],
    "capabilities": [
        {"id": "acme.orders:read", "provider": "acme", "name": "Orders (read)", "level": "read"},
        {
            "id": "acme.orders:write",
            "provider": "acme",
            "name": "Orders (write)",
            "level": "write",
            "implies": ["acme.orders:read"],
            "description": "Change orders.",
        },
    ],
    "operations": [
        {
            "id": "acme.orders.get",
            "service": "acme.orders",
            "method": "get",
            "path": "/v1/orders/{order_id}",
            "multi_segment": ["order_id"],
            "privilege": "read",
            "requires": [["acme.orders:read"]],
            "summary": "Get an order",
            "tags": ["orders"],
            "evidence": [
                {
                    "kind": "documented",
                    "source": "https://docs.example/orders",
                    "pointer": "#get",
                    "note": "From the API reference.",
                    "confidence": 0.9,
                }
            ],
        },
        {"id": "acme.tools.search", "service": "acme.tools", "mcp_tool": "search", "privilege": "read"},
    ],
    "concepts": [
        {
            "id": "orders.read",
            "name": "Read orders",
            "privilege": "read",
            "keywords": ["order", "orders"],
            "realized_by": ["acme.orders:read"],
            "description": "Look up orders.",
        }
    ],
    "concept_links": [{"concept": "orders.read", "realized_by": ["acme.orders:write"]}],
    "clients": [
        {
            "id": "acme-cli",
            "paths": ["/usr/bin/acme"],
            "description": "ACME client.",
            "bypasses_l7": True,
            "can_exfiltrate": False,
            "uses": [
                {"when_capability": ["acme.orders:read"], "operations": ["acme.orders.get"]},
                {"operations": ["acme.tools.search"]},
            ],
        }
    ],
}


def _doc(data, source="test.yaml", layer=Layer.ORG):
    return parse_catalog_document(copy.deepcopy(data), source=source, layer=layer)


def _graph(*docs):
    graph = Graph()
    for doc in docs:
        graph.add_document(doc)
    graph.finalize()
    return graph


def _without_evidence(doc):
    doc = copy.deepcopy(doc)
    for section in ("providers", "services", "capabilities", "operations", "concepts", "concept_links", "clients"):
        for node in getattr(doc, section):
            node.evidence = []
    return doc


@pytest.fixture(scope="module")
def base():
    return load_graph()


# --- parsing ---------------------------------------------------------------


def test_parse_reads_every_field_and_applies_defaults():
    doc = _doc(FULL_DOC, source="org/acme.yaml")
    assert doc.source == "org/acme.yaml"

    orders, tools = doc.services
    assert orders.inspection is Inspection.OPAQUE
    assert orders.origins == [Origin("orders.internal.example", 8443, "https")]
    assert orders.allowed_ips == ["10.20.0.0/16"]
    assert tools.protocol is Protocol.MCP
    assert tools.inspection is Inspection.L7
    assert tools.origins == [Origin("tools.internal.example", 443, "https")]

    get, search = doc.operations
    assert get.selector == RestSelector("GET", "/v1/orders/{order_id}")
    assert get.multi_segment == ["order_id"]
    assert get.requires == [["acme.orders:read"]]
    assert search.selector == McpSelector("search")
    assert search.requires == []
    assert search.privilege is Privilege.READ

    assert doc.capabilities[0].implies == []
    assert doc.capabilities[1].level is Privilege.WRITE
    assert doc.concept_links[0].realized_by == ["acme.orders:write"]

    client = doc.clients[0]
    assert client.bypasses_l7 is True and client.can_exfiltrate is False
    assert client.uses[1].when_capability == []

    defaults = _doc({"catalog_version": 1, "clients": [{"id": "x", "paths": ["/x"]}]}).clients[0]
    assert defaults.bypasses_l7 is False and defaults.can_exfiltrate is True


def test_parse_records_curated_and_explicit_evidence_and_layer():
    doc = _doc(FULL_DOC, source="org/acme.yaml", layer=Layer.AGENT)
    op = doc.operations[0]
    assert op.layer is Layer.AGENT
    assert op.evidence == [
        Evidence(EvidenceKind.CURATED, "catalog:org/acme.yaml", "operations[0]"),
        Evidence(EvidenceKind.DOCUMENTED, "https://docs.example/orders", "#get", "From the API reference.", 0.9),
    ]
    assert doc.clients[0].evidence == [Evidence(EvidenceKind.CURATED, "catalog:org/acme.yaml", "clients[0]")]


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"providers": []}, "catalog_version"),
        ({"catalog_version": 2}, "catalog_version"),
        ({"catalog_version": 1, "extra": []}, "extra"),
        ({"catalog_version": 1, "providers": [{"id": "a", "name": "A", "nmae": "typo"}]}, "nmae"),
        ({"catalog_version": 1, "providers": [{"name": "A"}]}, "providers[0]"),
        (
            {"catalog_version": 1, "operations": [{"id": "a.x", "service": "a.s", "privilege": "read"}]},
            "mcp_tool",
        ),
        (
            {
                "catalog_version": 1,
                "operations": [
                    {"id": "a.x", "service": "a.s", "method": "GET", "path": "/", "privilege": "read",
                     "requires": ["a.c:read"]}
                ],
            },
            "requires",
        ),
        ({"catalog_version": 1, "capabilities": [{"id": "a.c", "provider": "a", "name": "C", "level": "rw"}]}, "rw"),
    ],
)
def test_parse_rejects_malformed_documents(data, fragment):
    with pytest.raises(CatalogError, match=fragment.replace("[", r"\[")):
        parse_catalog_document(data, source="bad.yaml", layer=Layer.ORG)


def test_dump_round_trips_github_catalog():
    data = yaml.safe_load((BASE_CATALOG_DIR / "github.yaml").read_text())
    doc = parse_catalog_document(data, source="base/github.yaml", layer=Layer.BASE)
    dumped = dump_catalog_document(doc)
    assert dumped["catalog_version"] == 1
    reloaded = yaml.safe_load(yaml.safe_dump(dumped))
    again = parse_catalog_document(reloaded, source="base/github.yaml", layer=Layer.BASE)
    assert _without_evidence(again) == _without_evidence(doc)
    assert again == doc  # the curated record is regenerated, so evidence matches too


def test_dump_round_trips_every_field():
    doc = _doc(FULL_DOC, source="org/acme.yaml")
    dumped = dump_catalog_document(doc)
    assert dumped["operations"][0]["evidence"] == FULL_DOC["operations"][0]["evidence"]
    assert "evidence" not in dumped["providers"][0]  # only the generated curated record
    assert dumped["operations"][1]["requires"] == []  # always explicit
    assert _doc(dumped, source="org/acme.yaml") == doc


def test_load_catalog_file_labels_source_with_directory(tmp_path):
    catalog = tmp_path / "acme-catalog"
    catalog.mkdir()
    path = catalog / "acme.yaml"
    path.write_text(yaml.safe_dump(FULL_DOC))
    doc = load_catalog_file(path, layer=Layer.ORG)
    assert doc.source == "acme-catalog/acme.yaml"
    assert doc.providers[0].evidence[0].source == "catalog:acme-catalog/acme.yaml"

    path.write_text("catalog_version: [1\n")
    with pytest.raises(CatalogError, match="acme.yaml"):
        load_catalog_file(path, layer=Layer.ORG)


# --- merging and validation ------------------------------------------------


def test_base_catalog_loads_and_finalizes(base):
    assert {"github", "gitlab", "nvidia", "openai", "openshell"} <= set(base.providers)
    assert all(op.layer is Layer.BASE for op in base.operations.values())
    assert base.operations["github.repos.get"].evidence[0] == Evidence(
        EvidenceKind.CURATED, "catalog:base/github.yaml", "operations[0]"
    )
    assert base.diagnostics
    assert all(d.level == "info" for d in base.diagnostics)
    public = [d for d in base.diagnostics if d.code == "public-operation"]
    assert [d.message.split()[1] for d in public] == ["github.search.code"]


def test_duplicate_conflicting_id_raises_naming_both_sources():
    first = _doc(FULL_DOC, source="one.yaml")
    changed = copy.deepcopy(FULL_DOC)
    changed["capabilities"][0]["level"] = "write"
    second = _doc({"catalog_version": 1, "capabilities": changed["capabilities"][:1]}, source="two.yaml")
    graph = Graph()
    graph.add_document(first)
    with pytest.raises(CatalogError, match=r"acme\.orders:read.*one\.yaml.*two\.yaml"):
        graph.add_document(second)


def test_identical_duplicate_merges_evidence():
    data = {key: FULL_DOC[key] for key in ("catalog_version", "providers", "services", "capabilities", "operations")}
    graph = _graph(_doc(data, source="one.yaml"), _doc(data, source="two.yaml", layer=Layer.AGENT))
    op = graph.operations["acme.orders.get"]
    assert op.layer is Layer.ORG
    assert [ev.source for ev in op.evidence] == [
        "catalog:one.yaml",
        "https://docs.example/orders",
        "catalog:two.yaml",
    ]
    assert [ev.source for ev in graph.providers["acme"].evidence] == ["catalog:one.yaml", "catalog:two.yaml"]


@pytest.mark.parametrize("section", ["concepts", "clients"])
def test_any_duplicate_concept_or_client_raises(section):
    data = {key: FULL_DOC[key] for key in ("catalog_version", "providers", "capabilities", "operations", "services")}
    graph = Graph()
    graph.add_document(_doc(data, source="nodes.yaml"))
    graph.add_document(_doc({"catalog_version": 1, section: FULL_DOC[section]}, source="one.yaml"))
    with pytest.raises(CatalogError, match="one.yaml.*two.yaml"):
        graph.add_document(_doc({"catalog_version": 1, section: FULL_DOC[section]}, source="two.yaml"))


def test_graph_does_not_alias_document_nodes():
    doc = _doc(FULL_DOC)
    _graph(doc)
    assert doc.concepts[0].realized_by == ["acme.orders:read"]


@pytest.mark.parametrize(
    ("section", "entry"),
    [
        ("services", {"id": "other.orders", "provider": "acme", "name": "S", "origins": [{"host": "x"}]}),
        ("capabilities", {"id": "acmex:read", "provider": "acme", "name": "C", "level": "read"}),
    ],
)
def test_prefix_violation_raises_on_add(section, entry):
    graph = Graph()
    with pytest.raises(CatalogError, match="acme\\."):
        graph.add_document(_doc({"catalog_version": 1, section: [entry]}))


def test_operation_prefix_is_checked_against_its_service_provider_in_finalize():
    data = copy.deepcopy(FULL_DOC)
    data["operations"][0]["id"] = "orders.get"
    graph = Graph()
    graph.add_document(_doc(data))
    with pytest.raises(CatalogError, match="orders.get.*acme\\."):
        graph.finalize()


def test_dangling_references_are_listed_together():
    data = copy.deepcopy(FULL_DOC)
    data["services"][0]["provider"] = "nobody"
    data["services"][0]["id"] = "nobody.orders"
    data["capabilities"][1]["implies"] = ["acme.missing:read"]
    data["operations"][1]["requires"] = [["acme.tools:use"]]
    data["concepts"][0]["realized_by"] = ["acme.orders:read", "acme.ghost"]
    data["concept_links"].append({"concept": "no.such.concept", "realized_by": ["acme.orders:read"]})
    data["clients"][0]["uses"][0] = {"when_capability": ["acme.nope"], "operations": ["acme.orders.gone"]}
    graph = Graph()
    graph.add_document(_doc(data))
    with pytest.raises(CatalogError) as raised:
        graph.finalize()
    message = str(raised.value)
    for name in [
        "nobody",
        "acme.orders.get",  # its service acme.orders no longer exists
        "acme.missing:read",
        "acme.tools:use",
        "acme.ghost",
        "no.such.concept",
        "acme.nope",
        "acme.orders.gone",
    ]:
        assert name in message
    assert len(raised.value.problems) == 8


def test_finalize_keeps_reporting_a_link_to_an_unknown_concept():
    graph = Graph()
    graph.add_document(_doc({"catalog_version": 1, "concept_links": [{"concept": "nope", "realized_by": ["a.b"]}]}))
    for _ in range(2):
        with pytest.raises(CatalogError, match="nope"):
            graph.finalize()


# --- queries ---------------------------------------------------------------


def test_concept_links_from_gitlab_are_applied(base):
    realized = base.concepts["source-code.read"].realized_by
    assert realized == ["github.contents:read", "gitlab.read_repository"]
    assert base.capabilities_for_concept("source-code.read", provider="gitlab") == ["gitlab.read_repository"]
    assert base.capabilities_for_concept("no.such.concept") == []


def test_capability_closure_is_transitive(base):
    assert base.capability_closure({"github.contents:write"}) == {
        "github.contents:write",
        "github.contents:read",
        "github.metadata:read",
    }


def test_operations_enabled_by_follows_implies_and_skips_public_operations(base):
    enabled = [op.id for op in base.operations_enabled_by({"github.contents:write"})]
    assert "github.repos.get-content" in enabled
    assert "github.repos.put-content" in enabled
    assert "github.repos.get" in enabled  # contents:read implies metadata:read
    assert "github.search.code" not in enabled
    assert "github.issues.list" not in enabled
    assert enabled == [op_id for op_id in base.operations if op_id in set(enabled)]  # catalog order

    git_only = base.operations_enabled_by({"github.contents:read"}, services={"github.git"})
    assert [op.id for op in git_only] == ["github.git.info-refs", "github.git.upload-pack"]


def test_operations_enabled_by_uses_any_and_list(base):
    enabled = {op.id for op in base.operations_enabled_by({"github.pull_requests:write"})}
    assert "github.issues.create-comment" in enabled
    assert "github.issues.list-comments" in enabled
    assert "github.issues.create" not in enabled


def test_operations_enabled_by_requires_every_member_of_an_and_list():
    data = copy.deepcopy(FULL_DOC)
    data["operations"][0]["requires"] = [["acme.orders:read", "acme.orders:write"]]
    graph = _graph(_doc(data))
    assert graph.operations_enabled_by({"acme.orders:read"}) == []
    assert [op.id for op in graph.operations_enabled_by({"acme.orders:write"})] == ["acme.orders.get"]


def test_concepts_for_capability_includes_concepts_whose_capabilities_imply_it(base):
    concepts = base.concepts_for_capability("github.contents:read")
    assert "source-code.read" in concepts
    assert "source-code.write" in concepts
    assert "issues.read" not in concepts
    assert base.concepts_for_capability("gitlab.read_repository")[:2] == ["source-code.read", "source-code.write"]


def test_match_rest_narrows_to_declared_literals(base):
    matches = base.match_rest("api.github.com", 443, "GET", "/repos/NVIDIA/OpenShell/contents/{path}")
    assert [(op.id, glob) for op, glob in matches] == [
        ("github.repos.get-content", "/repos/NVIDIA/OpenShell/contents/**")
    ]
    assert base.match_rest("API.GitHub.com", 443, "get", "/repos/NVIDIA/OpenShell/contents/{path}") != []


def test_match_rest_any_method(base):
    matches = base.match_rest("api.github.com", 443, "*", "/repos/NVIDIA/OpenShell/contents/{path}")
    assert [op.id for op, _ in matches] == [
        "github.repos.get-content",
        "github.repos.put-content",
        "github.repos.delete-content",
    ]


def test_match_rest_unknown_host_or_port_is_empty(base):
    assert base.match_rest("example.com", 443, "GET", "/repos/a/b") == []
    assert base.match_rest("api.github.com", 80, "GET", "/repos/a/b") == []


def test_wildcard_origin_hosts_match_through_host_patterns():
    data = copy.deepcopy(FULL_DOC)
    data["services"][0]["origins"] = [{"host": "*.orders.example", "port": 443}]
    graph = _graph(_doc(data))
    assert [s.id for s in graph.services_for("EU.orders.example", 443)] == ["acme.orders"]
    assert graph.services_for("orders.example", 443) == []
    matches = graph.match_rest("eu.orders.example", 443, "GET", "/v1/orders/{id}")
    assert [(op.id, glob) for op, glob in matches] == [("acme.orders.get", "/v1/orders/**")]


def test_services_and_operations_lookups(base):
    assert [s.id for s in base.services_for("gitlab.com", 443)] == ["gitlab.rest", "gitlab.git"]
    assert [op.id for op in base.operations_of("github.codeload")] == ["github.codeload.get-archive"]
    assert base.operations_of("no.such.service") == []


def test_clients_for_path_matches_exactly(base):
    assert [c.id for c in base.clients_for_path("/usr/bin/git")] == ["git"]
    assert [c.id for c in base.clients_for_path("/usr/local/bin/node")] == ["node"]
    assert base.clients_for_path("/usr/bin/gi") == []


def test_queries_require_finalize():
    graph = Graph()
    graph.add_document(_doc(FULL_DOC))
    with pytest.raises(RuntimeError, match="finalize"):
        graph.services_for("orders.internal.example", 8443)


# --- load_graph ------------------------------------------------------------


def _write_catalog(directory, name, data):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(yaml.safe_dump(data))


def test_load_graph_layers_org_dirs_and_extra_documents(tmp_path):
    org = tmp_path / "acme-catalog"
    data = copy.deepcopy(FULL_DOC)
    data["concept_links"] = [{"concept": "source-code.read", "realized_by": ["acme.orders:read"]}]
    _write_catalog(org, "acme.yml", data)
    (org / "notes.txt").write_text("ignored")
    agent = CatalogDocument(
        providers=[Provider(id="agent", name="Agent API", layer=Layer.AGENT)],
        source="openapi:agent.yaml",
    )
    graph = load_graph([org], extra_documents=[agent])
    assert graph.providers["github"].layer is Layer.BASE
    assert graph.providers["acme"].layer is Layer.ORG
    assert graph.providers["agent"].layer is Layer.AGENT
    assert graph.concepts["source-code.read"].realized_by[-1] == "acme.orders:read"
    assert graph.operations["acme.orders.get"].evidence[0].source == "catalog:acme-catalog/acme.yml"


def test_load_graph_loads_files_in_sorted_order(tmp_path):
    org = tmp_path / "org"
    provider = {"catalog_version": 1, "providers": [{"id": "acme", "name": "ACME"}]}
    _write_catalog(org, "b.yaml", provider)
    _write_catalog(org, "a.yaml", {"catalog_version": 1, "providers": [{"id": "acme", "name": "Other"}]})
    with pytest.raises(CatalogError, match=r"org/a\.yaml.*org/b\.yaml"):
        load_graph([org], include_base=False)


def test_load_graph_missing_directory_raises(tmp_path):
    with pytest.raises(CatalogError, match="not found"):
        load_graph([tmp_path / "missing"])


def test_base_catalog_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "base"
    _write_catalog(custom, "acme.yaml", FULL_DOC)
    monkeypatch.setenv("POLICY_COMPILER_BASE_CATALOG", str(custom))
    graph = load_graph()
    assert set(graph.providers) == {"acme"}
    assert graph.providers["acme"].layer is Layer.BASE
    assert graph.providers["acme"].evidence[0].source == "catalog:base/acme.yaml"
    assert load_graph(include_base=False).providers == {}


def test_extra_documents_are_validated_against_earlier_layers():
    agent = CatalogDocument(
        operations=[
            Operation(
                id="github.repos.get-alias",
                service="github.rest",
                selector=RestSelector("GET", "/repos/{owner}/{repo}"),
                privilege=Privilege.READ,
                requires=[["github.metadata:read"]],
                layer=Layer.AGENT,
            )
        ],
        concepts=[Concept(id="x.read", name="X", privilege=Privilege.READ, realized_by=["github.nope"])],
        source="agent",
    )
    with pytest.raises(CatalogError, match="github.nope"):
        load_graph(extra_documents=[agent])
