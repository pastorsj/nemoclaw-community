# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from policy_compiler.graph.importers.github_permissions import import_github_permissions
from policy_compiler.graph.model import Origin, RestSelector
from policy_compiler.ir.common import EvidenceKind, Layer, Privilege

FIXTURE = Path(__file__).parent / "fixtures" / "github" / "fgpat-sample.yaml"
BASE_CATALOG = Path(__file__).parent.parent / "catalog" / "base" / "github.yaml"


@pytest.fixture(scope="module")
def doc():
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return import_github_permissions(data, source="fgpat-sample.yaml", layer=Layer.ORG)


def _ops(doc):
    return {op.id: op for op in doc.operations}


def _caps(doc):
    return {cap.id: cap for cap in doc.capabilities}


def test_provider_and_service_match_the_base_catalog(doc):
    base = yaml.safe_load(BASE_CATALOG.read_text(encoding="utf-8"))
    (provider,) = doc.providers
    assert (provider.id, provider.name) == ("github", "GitHub")
    assert provider.name == base["providers"][0]["name"]
    (service,) = doc.services
    base_rest = next(s for s in base["services"] if s["id"] == "github.rest")
    assert (service.id, service.provider, service.name) == ("github.rest", "github", base_rest["name"])
    assert service.origins == [Origin("api.github.com", 443, "https")]
    assert provider.layer is Layer.ORG and service.layer is Layer.ORG
    assert doc.source == "fgpat-sample.yaml"


def test_capabilities_and_implies_edges(doc):
    caps = _caps(doc)
    assert list(caps) == [
        "github.metadata:read",
        "github.contents:read",
        "github.contents:write",
        "github.issues:read",
        "github.issues:write",
        "github.pull_requests:read",
        "github.pull_requests:write",
        "github.workflows:write",
        "github.administration:read",
        "github.administration:write",
        "github.organization_custom_properties:read",
        "github.organization_custom_properties:admin",
    ]
    assert caps["github.contents:read"].name == "Contents (read)"
    assert caps["github.pull_requests:write"].name == "Pull requests (write)"
    assert caps["github.contents:write"].level is Privilege.WRITE
    assert caps["github.contents:write"].implies == ["github.contents:read"]
    assert caps["github.contents:read"].implies == []
    # No workflows:read in the data, so there is nothing to imply.
    assert caps["github.workflows:write"].implies == []
    # admin implies the next lower level that exists.
    assert caps["github.organization_custom_properties:admin"].level is Privilege.ADMIN
    assert caps["github.organization_custom_properties:admin"].implies == ["github.organization_custom_properties:read"]
    assert all(cap.provider == "github" for cap in doc.capabilities)


def test_capability_ids_and_levels_match_the_base_catalog(doc):
    base = yaml.safe_load(BASE_CATALOG.read_text(encoding="utf-8"))
    caps = _caps(doc)
    for base_cap in base["capabilities"]:
        if base_cap["id"] in caps:
            assert caps[base_cap["id"]].level.value == base_cap["level"], base_cap["id"]
            assert caps[base_cap["id"]].name == base_cap["name"], base_cap["id"]
    # Administration write is admin-level, as in the base catalog.
    assert caps["github.administration:write"].level is Privilege.ADMIN
    assert caps["github.administration:read"].level is Privilege.READ


def test_operations_are_deduplicated_by_verb_and_path(doc):
    ops = _ops(doc)
    assert list(ops) == [
        "github.repos.get-a-repository",
        "github.markdown.render-a-markdown-document",
        "github.repos.get-repository-content",
        "github.repos.create-or-update-file-contents",
        "github.issues.list-issue-comments",
        "github.issues.create-an-issue-comment",
        "github.actions.get-github-actions-cache-retention-limit-for-a-repository",
        "github.repos.delete-a-repository",
        "github.orgs.get-all-custom-properties-for-an-organization",
        "github.orgs.create-or-update-custom-properties-for-an-organization",
    ]
    content = ops["github.repos.get-repository-content"]
    assert content.service == "github.rest"
    assert content.selector == RestSelector("GET", "/repos/{owner}/{repo}/contents/{path}")
    assert content.multi_segment == ["path"]
    assert content.requires == [["github.contents:read"]]
    assert content.privilege is Privilege.READ
    assert content.layer is Layer.ORG
    assert ops["github.repos.get-a-repository"].multi_segment == []


AND_NOTE = (
    "GitHub lists additional permissions; required all of them because the flat data cannot distinguish OR from AND"
)


def _record(perm_access, verb, path, *, additional, slug="op"):
    return {
        "category": "repos",
        "slug": slug,
        "subcategory": "repos",
        "verb": verb,
        "requestPath": path,
        "additional-permissions": additional,
        "access": perm_access,
    }


def test_additional_permissions_fail_safe_to_one_and_list(doc):
    ops = _ops(doc)
    # Every entry carries additional-permissions: true, so all distinct capabilities are required together.
    comments = ops["github.issues.list-issue-comments"]
    assert comments.requires == [["github.issues:read", "github.pull_requests:read"]]
    assert comments.privilege is Privilege.READ
    create = ops["github.issues.create-an-issue-comment"]
    assert create.requires == [["github.issues:write", "github.pull_requests:write"]]
    assert create.privilege is Privilege.WRITE
    # The duplicate contents record collapses into one member of the AND-list.
    put = ops["github.repos.create-or-update-file-contents"]
    assert put.requires == [["github.contents:write", "github.workflows:write"]]
    assert put.multi_segment == ["path"]
    for op in (comments, create, put):
        assert all(e.note == AND_NOTE for e in op.evidence), op.id
    # Endpoints without additional permissions keep a plain requirement and no note.
    content = ops["github.repos.get-repository-content"]
    assert content.requires == [["github.contents:read"]]
    assert all(e.note == "" for e in content.evidence)


def test_independent_permissions_stay_an_or_with_the_maximum_privilege():
    data = {
        "metadata": {"title": "Metadata", "permissions": [_record("read", "get", "/x", additional=False)]},
        "contents": {"title": "Contents", "permissions": [_record("write", "get", "/x", additional=False)]},
    }
    (op,) = import_github_permissions(data, source="x.json", layer=Layer.ORG).operations
    assert op.requires == [["github.metadata:read"], ["github.contents:write"]]
    # Privilege is the maximum across entries, so the level is never understated.
    assert op.privilege is Privilege.WRITE
    assert all(e.note == "" for e in op.evidence)


def test_fork_style_endpoint_is_not_enabled_by_contents_read_alone():
    path = "/repos/{owner}/{repo}/forks"
    data = {
        "administration": {"title": "Administration", "permissions": [_record("write", "post", path, additional=True)]},
        "contents": {
            "title": "Contents",
            "permissions": [
                _record("read", "post", path, additional=True),
                _record("read", "post", path, additional=True),
            ],
        },
        "repository_creation": {
            "title": "Repository creation",
            "permissions": [_record("write", "post", path, additional=True)],
        },
    }
    (op,) = import_github_permissions(data, source="x.json", layer=Layer.ORG).operations
    assert op.requires == [["github.administration:write", "github.contents:read", "github.repository_creation:write"]]
    # No alternative is satisfied by contents:read on its own.
    assert not any(set(and_set) <= {"github.contents:read"} for and_set in op.requires)
    assert op.privilege is Privilege.ADMIN


def test_additional_permissions_missing_from_the_data_keep_the_listed_permission():
    path = "/orgs/{org}/teams/{team_slug}/repos/{owner}/{repo}"
    data = {"members": {"title": "Members", "permissions": [_record("read", "get", path, additional=True)]}}
    (op,) = import_github_permissions(data, source="x.json", layer=Layer.ORG).operations
    assert op.requires == [["github.members:read"]]
    assert op.privilege is Privilege.READ
    assert [e.note for e in op.evidence] == [
        "GitHub lists additional permissions that are not present in this data; only the listed permission is required"
    ]


def test_privilege_is_the_maximum_capability_level(doc):
    ops = _ops(doc)
    # A POST that GitHub classifies as read.
    assert ops["github.markdown.render-a-markdown-document"].privilege is Privilege.READ
    assert ops["github.repos.delete-a-repository"].privilege is Privilege.ADMIN
    assert ops["github.repos.delete-a-repository"].requires == [["github.administration:write"]]
    assert ops["github.orgs.create-or-update-custom-properties-for-an-organization"].privilege is Privilege.ADMIN


def test_evidence_is_documented(doc):
    ops = _ops(doc)
    comments = ops["github.issues.list-issue-comments"]
    assert [(e.kind, e.source, e.pointer) for e in comments.evidence] == [
        (EvidenceKind.DOCUMENTED, "github-docs:fgpat-sample.yaml", "issues.permissions[0]"),
        (EvidenceKind.DOCUMENTED, "github-docs:fgpat-sample.yaml", "pull_requests.permissions[0]"),
    ]
    for node in [*doc.providers, *doc.services, *doc.capabilities, *doc.operations]:
        assert node.evidence, node.id
        assert all(e.kind is EvidenceKind.DOCUMENTED for e in node.evidence), node.id
        assert all(e.source == "github-docs:fgpat-sample.yaml" for e in node.evidence), node.id
    assert _caps(doc)["github.contents:read"].evidence[0].pointer == "contents"


def test_summary_and_tags_come_from_slug_and_category(doc):
    op = _ops(doc)["github.issues.list-issue-comments"]
    assert op.summary == "List issue comments"
    assert op.tags == ["issues", "comments"]


def test_conflicting_ids_are_suffixed_deterministically():
    record = {
        "category": "repos",
        "slug": "get",
        "subcategory": "repos",
        "access": "read",
        "additional-permissions": False,
    }
    data = {
        "metadata": {
            "title": "Metadata",
            "permissions": [
                {**record, "verb": "get", "requestPath": "/a"},
                {**record, "verb": "get", "requestPath": "/b"},
                {**record, "verb": "head", "requestPath": "/a"},
            ],
        }
    }
    doc = import_github_permissions(data, source="github-docs:x.json", layer=Layer.BASE)
    assert [(op.id, op.selector.method, op.selector.path) for op in doc.operations] == [
        ("github.repos.get", "GET", "/a"),
        ("github.repos.get-2", "GET", "/b"),
        ("github.repos.get-3", "HEAD", "/a"),
    ]
    # A source that already carries the prefix is kept as is.
    assert doc.operations[0].evidence[0].source == "github-docs:x.json"


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"contents": {"title": "Contents"}},
        {"contents": {"title": "Contents", "permissions": [{"verb": "get", "requestPath": "/a"}]}},
        {
            "contents": {
                "title": "Contents",
                "permissions": [{"verb": "get", "requestPath": "/a", "access": "owner", "category": "c", "slug": "s"}],
            }
        },
    ],
)
def test_malformed_data_is_rejected(data):
    with pytest.raises(ValueError):
        import_github_permissions(data, source="bad.json", layer=Layer.ORG)
