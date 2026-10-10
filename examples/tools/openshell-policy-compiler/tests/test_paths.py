# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from policy_compiler import paths


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("/repos/acme/x/**", "/repos/acme/x/contents/a/b.md", True),
        ("/repos/acme/x/**", "/repos/acme/x", False),  # documented OpenShell behavior
        ("/repos/*/x", "/repos/acme/x", True),
        ("/repos/*/x", "/repos/acme/y", False),
        ("/repos/*", "/repos/acme/x", False),
        ("/bot*/sendMessage", "/bot123/sendMessage", True),
        ("/**", "/anything/at/all", True),
        ("/v1/chat/completions", "/v1/chat/completions", True),
    ],
)
def test_glob_matches(glob, path, expected):
    assert paths.glob_matches(glob, path) is expected


def test_normalize_agentspec_template():
    assert paths.normalize_agentspec_template("/a/{{ path }}/{{x}}") == "/a/{path}/{x}"
    assert paths.has_placeholder("https://{{host}}/x")
    assert not paths.has_placeholder("https://api.github.com/x")


def test_instantiate_binds_and_wildcards():
    template = "/repos/{owner}/{repo}/contents/{path}"
    assert paths.instantiate(template, {"owner": "NVIDIA", "repo": "OpenShell"}, {"path"}) == (
        "/repos/NVIDIA/OpenShell/contents/**"
    )
    assert paths.instantiate(template) == "/repos/*/*/contents/*"
    assert paths.instantiate("/files/v{n}.json") == "/files/v*.json"


def test_align_keeps_declared_literals_and_operation_shape():
    declared = paths.normalize_agentspec_template("/repos/NVIDIA/OpenShell/contents/{{path}}")
    template = "/repos/{owner}/{repo}/contents/{path}"
    assert paths.align(declared, template, {"path"}) == "/repos/NVIDIA/OpenShell/contents/**"
    assert paths.align("/repos/NVIDIA/OpenShell", "/repos/{owner}/{repo}") == "/repos/NVIDIA/OpenShell"
    assert paths.align("/repos/NVIDIA/OpenShell/issues", "/repos/{owner}/{repo}") is None
    # A literal under a multi-segment parameter stays literal.
    assert paths.align("/repos/a/b/contents/docs/x.md", template, {"path"}) == "/repos/a/b/contents/docs/x.md"


def test_overlap_and_cover():
    t = paths.parse_template("/repos/{owner}/{repo}/contents/{path}", {"path"})
    assert paths.overlaps(paths.parse_glob("/repos/acme/x/**"), t)
    assert not paths.covers(paths.parse_glob("/repos/acme/x/**"), t)
    assert paths.covers(paths.parse_glob("/repos/**"), t)
    assert not paths.overlaps(paths.parse_glob("/orgs/**"), t)
    assert paths.glob_covers("/repos/acme/**", "/repos/acme/x/*")
    assert not paths.glob_covers("/repos/acme/x/*", "/repos/acme/**")
    # ** needs at least one segment on both sides.
    assert paths.overlaps(paths.parse_glob("/a/**/c"), paths.parse_glob("/a/b/**"))


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        ("api.github.com", "API.github.com", True),
        ("*.github.com", "api.github.com", True),
        ("*.github.com", "a.b.github.com", False),
        ("**.github.com", "a.b.github.com", True),
        ("**.github.com", "github.com", False),
    ],
)
def test_host_matches(pattern, host, expected):
    assert paths.host_matches(pattern, host) is expected


def test_method_covers():
    assert paths.method_covers("*", "DELETE")
    assert paths.method_covers("GET", "HEAD")
    assert not paths.method_covers("GET", "POST")
