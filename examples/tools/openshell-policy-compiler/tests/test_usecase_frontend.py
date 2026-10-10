# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from policy_compiler.frontends.usecase import contract_from_use_case
from policy_compiler.graph.model import CatalogDocument, Provider
from policy_compiler.graph.store import Graph, load_graph
from policy_compiler.ir.common import Evidence, EvidenceKind, Privilege
from policy_compiler.ir.contract import Target, UnknownKind

USER_EXAMPLE = "I need access to code in GitHub so that I can understand NVIDIA/OpenShell"
CLIENTS = ["/usr/bin/python3"]
NVIDIA_OPENSHELL = {"owner": "NVIDIA", "repo": "OpenShell"}


@pytest.fixture(scope="module")
def graph():
    return load_graph()


def compile_text(graph, text, **kwargs):
    kwargs.setdefault("default_clients", CLIENTS)
    return contract_from_use_case(text, graph, **kwargs)


def concept_needs(contract):
    return [need for need in contract.needs if need.target.concept is not None]


def concept_targets(contract):
    return [(n.target.concept, n.target.provider, n.target.scope) for n in concept_needs(contract)]


def capability_needs(contract):
    return [need.target.capability for need in contract.needs if need.target.capability is not None]


# --- the user's example --------------------------------------------------------


def test_user_example(graph):
    contract = compile_text(graph, USER_EXAMPLE)
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    for need in concept_needs(contract):
        assert graph.concepts[need.target.concept].privilege == Privilege.READ
    assert contract.unknowns == []


def test_user_example_need_details(graph):
    need = concept_needs(compile_text(graph, USER_EXAMPLE))[0]
    assert need.id == "need-01-source-code-read"
    assert need.target == Target(concept="source-code.read", provider="github", scope=NVIDIA_OPENSHELL)
    assert need.rationale == "use case: matched code, understand"
    assert need.evidence == [Evidence(EvidenceKind.USER, "use-case", note=USER_EXAMPLE)]
    assert need.clients == []


def test_contract_fields(graph):
    contract = compile_text(graph, USER_EXAMPLE, agent_name="explainer", default_clients=["/usr/bin/git"])
    assert contract.agent == "explainer"
    assert contract.use_case == USER_EXAMPLE
    assert contract.default_clients == ["/usr/bin/git"]
    assert contract.sources == ["use-case"]
    assert contract.prohibitions == []


def test_default_clients_are_copied(graph):
    clients = ["/usr/bin/git"]
    contract = compile_text(graph, USER_EXAMPLE, default_clients=clients)
    clients.append("/usr/bin/curl")
    assert contract.default_clients == ["/usr/bin/git"]


def test_dropped_write_concepts_are_noted(graph):
    contract = compile_text(graph, USER_EXAMPLE)
    notes = [note for note in contract.notes if "write" in note]
    assert len(notes) == 1
    assert "source-code.write" in notes[0]


def test_contract_is_deterministic(graph):
    text = "open issues for bugs you find in NVIDIA/OpenShell on GitHub and read the code"
    assert compile_text(graph, text) == compile_text(graph, text)


# --- provider choice -----------------------------------------------------------


def test_no_provider_named_is_ambiguous(graph):
    contract = compile_text(graph, "access to code")
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    ambiguous = [u for u in contract.unknowns if u.kind == UnknownKind.AMBIGUOUS]
    assert len(ambiguous) == 1
    unknown = ambiguous[0]
    assert unknown.subject == "source-code.read"
    assert "github" in unknown.description and "gitlab" in unknown.description
    assert unknown.id == "unknown-01-source-code-read"
    assert [s.concept for s in unknown.suggestions] == ["source-code.read"]
    assert unknown.evidence == [Evidence(EvidenceKind.USER, "use-case", note="access to code")]


def test_single_realizing_provider_is_used(graph):
    # Only GitHub realizes code-review.read in the base catalog.
    contract = compile_text(graph, "review pull requests in NVIDIA/OpenShell")
    assert concept_targets(contract) == [("code-review.read", "github", NVIDIA_OPENSHELL)]


def test_one_need_per_mentioned_provider(graph):
    contract = compile_text(graph, "read code on GitHub and GitLab")
    assert concept_targets(contract) == [("source-code.read", "github", {}), ("source-code.read", "gitlab", {})]
    assert [n.id for n in contract.needs] == [
        "need-01-source-code-read",
        "need-02-source-code-read",
        "need-03-inference",
    ]


def test_mentioned_providers_that_do_not_realize_the_concept_are_skipped(graph):
    contract = compile_text(graph, "use OpenAI models to understand code on GitHub", inference="openai")
    assert concept_targets(contract) == [("source-code.read", "github", {})]


def test_naming_a_rival_provider_that_lacks_the_concept_is_reported(graph):
    # GitLab offers source code and issues, but the catalog has no GitLab
    # realization of code-review.read; granting GitHub instead would be wrong.
    contract = compile_text(graph, "review pull requests on GitLab")
    assert concept_needs(contract) == []
    [unknown] = contract.unknowns
    assert unknown.kind == UnknownKind.NOT_IN_CATALOG
    assert unknown.subject == "code-review.read"
    assert "gitlab" in unknown.description and "github" in unknown.description


def test_repository_owner_that_is_a_provider_name_is_not_a_provider_mention(graph):
    # "NVIDIA" and "OpenShell" are provider IDs, but inside NVIDIA/OpenShell they
    # name a repository, not a provider, so the source-code concept stays ambiguous.
    contract = compile_text(graph, "understand the code of NVIDIA/OpenShell")
    assert concept_needs(contract) == []
    assert [u.kind for u in contract.unknowns] == [UnknownKind.AMBIGUOUS]


# --- privilege -----------------------------------------------------------------


def test_open_issues_includes_issues_write(graph):
    contract = compile_text(graph, "open issues for bugs you find in NVIDIA/OpenShell on GitHub")
    targets = concept_targets(contract)
    assert ("issues.write", "github", NVIDIA_OPENSHELL) in targets
    assert {concept for concept, _, _ in targets} <= {"issues.write", "issues.read", "source-code.read"}


def test_write_verb_for_one_concept_does_not_license_another(graph):
    # "open" asks for issues.write; it must not keep source-code.write, which
    # matched only "code".
    contract = compile_text(graph, "read the code and open issues for bugs in NVIDIA/OpenShell on GitHub")
    concepts = [concept for concept, _, _ in concept_targets(contract)]
    assert "source-code.read" in concepts
    assert "issues.write" in concepts
    assert "source-code.write" not in concepts
    assert "code-review.write" not in concepts


def test_concepts_explained_by_a_stronger_match_are_dropped(graph):
    contract = compile_text(graph, "open issues for bugs you find in NVIDIA/OpenShell on GitHub")
    assert "code-review.write" not in [c for c, _, _ in concept_targets(contract)]
    assert any("code-review.write" in note and "issues.write" in note for note in contract.notes)


def test_explicit_write_request_keeps_write_concept(graph):
    contract = compile_text(graph, "fix bugs and push commits to NVIDIA/OpenShell on GitHub")
    assert ("source-code.write", "github", NVIDIA_OPENSHELL) in concept_targets(contract)


def test_reading_source_files_is_not_a_write_request(graph):
    text = "Summarizes the architecture of a repository by reading its source files on GitHub"
    contract = compile_text(graph, text)
    assert concept_targets(contract) == [("source-code.read", "github", {})]


def test_negated_write_verbs_grant_nothing(graph):
    contract = compile_text(graph, "read the code of NVIDIA/OpenShell on GitHub but never push commits")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]
    assert any("push" in note for note in contract.notes)


# --- scope ---------------------------------------------------------------------


def test_github_url_scope(graph):
    contract = compile_text(graph, "understand the code at https://github.com/NVIDIA/OpenShell/tree/main/src")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


def test_github_url_with_git_suffix(graph):
    contract = compile_text(graph, "clone github.com/NVIDIA/OpenShell.git and read the code.")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


def test_gitlab_url_scope(graph):
    contract = compile_text(graph, "read the code in https://gitlab.com/gitlab-org/gitlab-runner")
    assert concept_targets(contract) == [
        (
            "source-code.read",
            "gitlab",
            {"id": "gitlab-org%2Fgitlab-runner", "namespace": "gitlab-org", "project": "gitlab-runner"},
        )
    ]


def test_gitlab_url_with_subgroup_and_page(graph):
    contract = compile_text(graph, "read the code in https://gitlab.com/acme/platform/api/-/tree/main")
    assert concept_targets(contract) == [
        (
            "source-code.read",
            "gitlab",
            {"id": "acme%2Fplatform%2Fapi", "namespace": "acme/platform", "project": "api"},
        )
    ]


def test_bare_repository_on_gitlab(graph):
    contract = compile_text(graph, "read the code of gitlab-org/gitlab on GitLab")
    assert concept_targets(contract) == [
        ("source-code.read", "gitlab", {"id": "gitlab-org%2Fgitlab", "namespace": "gitlab-org", "project": "gitlab"})
    ]


def test_url_scope_applies_only_to_its_provider(graph):
    contract = compile_text(graph, "read the code in github.com/NVIDIA/OpenShell and on GitLab")
    assert concept_targets(contract) == [
        ("source-code.read", "github", NVIDIA_OPENSHELL),
        ("source-code.read", "gitlab", {}),
    ]


def test_several_repositories_give_several_needs(graph):
    contract = compile_text(graph, "read the code of NVIDIA/OpenShell and NVIDIA/NemoClaw on GitHub")
    assert concept_targets(contract) == [
        ("source-code.read", "github", NVIDIA_OPENSHELL),
        ("source-code.read", "github", {"owner": "NVIDIA", "repo": "NemoClaw"}),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "read and/or understand code on GitHub",
        "read code on GitHub before 10/10/2026",
        "read code on GitHub, input/output is fine",
        "read client/server code on GitHub",
        "read code on GitHub via TCP/IP",
        "read code on GitHub (see example.com/docs)",
        "read code on GitHub w/ care",
    ],
)
def test_slash_words_are_not_repositories(graph, text):
    contract = compile_text(graph, text)
    assert concept_targets(contract) == [("source-code.read", "github", {})]


def test_possessive_and_punctuation_around_repository(graph):
    contract = compile_text(graph, "Explain (NVIDIA/OpenShell's) code on GitHub.")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


# --- inference -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("inference", "capability"),
    [
        ("local", "openshell.inference:invoke"),
        ("nvidia", "nvidia.inference:invoke"),
        ("openai", "openai.model:request"),
    ],
)
def test_inference_need(graph, inference, capability):
    contract = compile_text(graph, USER_EXAMPLE, inference=inference)
    [need] = [n for n in contract.needs if n.target.capability is not None]
    assert need.target == Target(capability=capability)
    assert "model" in need.rationale
    assert need.id == "need-02-inference"
    assert need.evidence[0].kind == EvidenceKind.USER


def test_no_inference(graph):
    contract = compile_text(graph, USER_EXAMPLE, inference="none")
    assert capability_needs(contract) == []
    assert [n.id for n in contract.needs] == ["need-01-source-code-read"]


def test_unknown_inference_choice_raises(graph):
    with pytest.raises(ValueError, match="inference"):
        compile_text(graph, USER_EXAMPLE, inference="gemini")


def test_model_words_are_covered_by_the_inference_need(graph):
    contract = compile_text(graph, "summarize the code of NVIDIA/OpenShell on GitHub with a model")
    assert [c for c, _, _ in concept_targets(contract)] == ["source-code.read"]
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    assert contract.unknowns == []


def test_model_words_without_inference_need_ask_for_a_provider(graph):
    contract = compile_text(graph, "summarize text with a language model", inference="none")
    assert contract.needs == []
    assert [(u.kind, u.subject) for u in contract.unknowns] == [(UnknownKind.AMBIGUOUS, "llm.inference")]


def test_inference_capability_missing_from_the_catalog_is_unknown():
    graph = Graph()
    graph.add_document(CatalogDocument(providers=[Provider(id="acme", name="ACME")], source="test"))
    graph.finalize()
    contract = contract_from_use_case("read orders", graph, default_clients=CLIENTS)
    assert contract.needs == []
    [unknown] = contract.unknowns
    assert (unknown.kind, unknown.subject) == (UnknownKind.NOT_IN_CATALOG, "openshell.inference:invoke")


def test_nothing_matched_is_noted(graph):
    contract = compile_text(graph, "zxqv plorbs")
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    assert concept_needs(contract) == []
    assert any("No concept" in note for note in contract.notes)
