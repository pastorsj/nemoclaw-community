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
    assert "repository" in unknown.description


def test_a_provider_is_never_chosen_silently(graph):
    # Only GitHub realizes code-review.read in the base catalog, but the text does not name GitHub.
    contract = compile_text(graph, "review pull requests in NVIDIA/OpenShell")
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    [unknown] = contract.unknowns
    assert (unknown.kind, unknown.subject) == (UnknownKind.AMBIGUOUS, "code-review.read")
    assert "github" in unknown.description and "names none" in unknown.description


def test_services_outside_the_catalog_never_map_to_a_catalog_provider(graph):
    contract = compile_text(graph, "Read tickets from Jira project ABC and post to Slack")
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    assert contract.unknowns
    assert {u.kind for u in contract.unknowns} == {UnknownKind.NOT_IN_CATALOG}
    issues = next(u for u in contract.unknowns if u.subject == "issues.read")
    assert "Jira" in issues.description and "Slack" in issues.description


def test_a_concept_scoped_to_repositories_needs_a_repository(graph):
    contract = compile_text(graph, "read the code on GitHub")
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    [unknown] = contract.unknowns
    assert (unknown.kind, unknown.subject) == (UnknownKind.AMBIGUOUS, "source-code.read")
    assert "repository" in unknown.description and "github" in unknown.description


def test_owner_only_scope_is_not_a_missing_repository(graph):
    contract = compile_text(graph, "read the code of github.com/NVIDIA")
    assert concept_targets(contract) == [("source-code.read", "github", {"owner": "NVIDIA"})]
    assert contract.unknowns == []


def test_deleting_a_repository_is_not_in_the_catalog(graph):
    contract = compile_text(graph, "Delete the GitHub repository NVIDIA/OpenShell")
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    [unknown] = contract.unknowns
    assert unknown.kind == UnknownKind.NOT_IN_CATALOG
    assert "delete" in unknown.description and "repository" in unknown.description


def test_deleting_files_is_a_source_code_write(graph):
    contract = compile_text(graph, "delete files in NVIDIA/OpenShell on GitHub")
    assert ("source-code.write", "github", NVIDIA_OPENSHELL) in concept_targets(contract)
    assert contract.unknowns == []


def test_a_write_verb_from_an_operation_summary_needs_that_operations_object(graph):
    # "Delete a file" lets "delete" ask for source-code.write, but only for files.
    contract = compile_text(graph, "push commits and delete the repository NVIDIA/OpenShell on GitHub")
    assert [c for c, _, _ in concept_targets(contract)] == ["source-code.write"]
    assert [(u.kind, "delete" in u.description) for u in contract.unknowns] == [(UnknownKind.NOT_IN_CATALOG, True)]


@pytest.mark.parametrize(
    "text",
    [
        "full access to GitHub NVIDIA/OpenShell",
        "give the agent admin access to NVIDIA/OpenShell on GitHub",
        "unrestricted access to GitHub",
    ],
)
def test_full_access_is_never_granted(graph, text):
    contract = compile_text(graph, text)
    assert concept_needs(contract) == []
    assert capability_needs(contract) == ["openshell.inference:invoke"]
    [unknown] = contract.unknowns
    assert unknown.kind == UnknownKind.AMBIGUOUS
    assert "never granted" in unknown.description and "github" in unknown.description


def test_full_access_with_named_actions_grants_only_those_actions(graph):
    contract = compile_text(graph, "full access to read the code of NVIDIA/OpenShell on GitHub")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]
    assert [u.kind for u in contract.unknowns] == [UnknownKind.AMBIGUOUS]
    assert "never granted" in contract.unknowns[0].description


def test_one_need_per_mentioned_provider(graph):
    contract = compile_text(graph, "read code of NVIDIA/OpenShell on GitHub and GitLab")
    assert concept_targets(contract) == [
        ("source-code.read", "github", NVIDIA_OPENSHELL),
        ("source-code.read", "gitlab", {"id": "NVIDIA%2FOpenShell", "namespace": "NVIDIA", "project": "OpenShell"}),
    ]
    assert [n.id for n in contract.needs] == [
        "need-01-source-code-read",
        "need-02-source-code-read",
        "need-03-inference",
    ]


def test_mentioned_providers_that_do_not_realize_the_concept_are_skipped(graph):
    contract = compile_text(graph, "use OpenAI models to understand code of NVIDIA/OpenShell on GitHub", inference="openai")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


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


def test_a_write_verb_needs_a_matching_object(graph):
    # "open" is an action verb of code-review.write, but its object here is issues.
    contract = compile_text(graph, "open issues for bugs you find in NVIDIA/OpenShell on GitHub")
    assert "code-review.write" not in [c for c, _, _ in concept_targets(contract)]
    assert any("code-review.write" in note for note in contract.notes)


READ_ONLY_PHRASINGS = [
    "list open issues in NVIDIA/OpenShell on GitHub",
    "summarize open pull requests in NVIDIA/OpenShell on GitHub",
    "list merged pull requests in NVIDIA/OpenShell on GitHub",
    "which issues were opened last week in NVIDIA/OpenShell on GitHub",
    "find changed files in pull requests of NVIDIA/OpenShell on GitHub",
    "opening issues is not allowed; read issues in NVIDIA/OpenShell on GitHub",
    "show recently updated issues in NVIDIA/OpenShell on GitHub",
    "open the code of NVIDIA/OpenShell on GitHub",
    "I need read/write access to NVIDIA/OpenShell on GitHub",
    "review the commit history of NVIDIA/OpenShell on GitHub",
]


@pytest.mark.parametrize("text", READ_ONLY_PHRASINGS)
def test_read_only_phrasings_grant_no_write_concept(graph, text):
    contract = compile_text(graph, text)
    write_level = [
        need.target.concept
        for need in concept_needs(contract)
        if graph.concepts[need.target.concept].privilege.at_least(Privilege.WRITE)
    ]
    assert write_level == []


def test_open_the_code_keeps_source_code_read(graph):
    contract = compile_text(graph, "open the code of NVIDIA/OpenShell on GitHub")
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


def test_show_recently_updated_issues_keeps_issues_read(graph):
    contract = compile_text(graph, "show recently updated issues in NVIDIA/OpenShell on GitHub")
    assert concept_targets(contract) == [("issues.read", "github", NVIDIA_OPENSHELL)]


def test_negation_applies_to_one_occurrence(graph):
    contract = compile_text(graph, "open issues but never open pull requests in NVIDIA/OpenShell on GitHub")
    concepts = [c for c, _, _ in concept_targets(contract)]
    assert "issues.write" in concepts
    assert "code-review.write" not in concepts
    assert any("open pull request" in note for note in contract.notes)


def test_comment_on_pull_requests_grants_code_review_write(graph):
    # "Comment on an issue or pull request" is a write operation that pull
    # request write access enables, so "comment" is an action verb of code-review.write.
    contract = compile_text(graph, "comment on pull requests in NVIDIA/OpenShell on GitHub")
    concepts = [c for c, _, _ in concept_targets(contract)]
    assert "code-review.write" in concepts
    assert "issues.write" not in concepts


def test_redundancy_never_trades_read_for_write(graph):
    contract = compile_text(graph, "edit the code of NVIDIA/OpenShell on GitHub")
    concepts = [c for c, _, _ in concept_targets(contract)]
    assert concepts == ["source-code.write", "source-code.read"]


def test_redundancy_drops_a_concept_covered_by_capabilities(graph):
    # contents:read implies metadata:read on GitHub, so source-code.read covers
    # repository.metadata.read; "language" alone does not justify a second need.
    text = "read the code and the languages of NVIDIA/OpenShell on GitHub"
    contract = compile_text(graph, text)
    assert [c for c, _, _ in concept_targets(contract)] == ["source-code.read"]
    assert any("repository.metadata.read (covered by source-code.read)" in note for note in contract.notes)


def test_concepts_need_a_keyword_naming_what_to_access(graph):
    # "review" is a keyword of issues.read, but it is only a verb here.
    contract = compile_text(graph, "review pull requests in NVIDIA/OpenShell on GitHub")
    assert [c for c, _, _ in concept_targets(contract)] == ["code-review.read"]
    assert any("issues.read" in note for note in contract.notes)


def test_explicit_write_request_keeps_write_concept(graph):
    contract = compile_text(graph, "fix bugs and push commits to NVIDIA/OpenShell on GitHub")
    assert ("source-code.write", "github", NVIDIA_OPENSHELL) in concept_targets(contract)


def test_reading_source_files_is_not_a_write_request(graph):
    text = "Summarizes the architecture of NVIDIA/OpenShell by reading its source files on GitHub"
    contract = compile_text(graph, text)
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]


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
    assert concept_targets(contract) == [("source-code.read", "github", NVIDIA_OPENSHELL)]
    [unknown] = contract.unknowns
    assert (unknown.kind, unknown.subject) == (UnknownKind.AMBIGUOUS, "source-code.read")
    assert "gitlab" in unknown.description and "repository" in unknown.description


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
        "read code and issues/PRs on GitHub",
        "read the code in docs/README on GitHub",
        "read the code in lib/utils.py on GitHub",
        "read the code in src/main on GitHub",
        "read the code on GitHub, see github.com/settings/tokens",
        "read the code on GitHub, see https://github.com/marketplace/actions",
    ],
)
def test_slash_words_are_not_repositories(graph, text):
    # No repository is named, so nothing is granted and the compiler asks for one.
    contract = compile_text(graph, text)
    assert concept_needs(contract) == []
    asked = [u for u in contract.unknowns if u.subject == "source-code.read"]
    assert [u.kind for u in asked] == [UnknownKind.AMBIGUOUS]
    assert "names no repository" in asked[0].description


def test_owner_only_url_scopes_to_the_owner_with_a_note(graph):
    contract = compile_text(graph, "read the code at https://github.com/NVIDIA")
    assert concept_targets(contract) == [("source-code.read", "github", {"owner": "NVIDIA"})]
    assert any("not limited to one repository" in note and "NVIDIA" in note for note in contract.notes)


def test_org_url_scopes_to_the_owner(graph):
    contract = compile_text(graph, "read the code at github.com/orgs/NVIDIA/repositories")
    assert concept_targets(contract) == [("source-code.read", "github", {"owner": "NVIDIA"})]


def test_owner_only_gitlab_url_scopes_to_the_group(graph):
    contract = compile_text(graph, "read the code at https://gitlab.com/gitlab-org")
    assert concept_targets(contract) == [("source-code.read", "gitlab", {"namespace": "gitlab-org"})]
    assert any("not limited to one repository" in note for note in contract.notes)


def test_repository_names_with_dots_are_kept(graph):
    contract = compile_text(graph, "read the code of vercel/next.js on GitHub")
    assert concept_targets(contract) == [("source-code.read", "github", {"owner": "vercel", "repo": "next.js"})]


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
