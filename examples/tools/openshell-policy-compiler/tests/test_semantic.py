# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from policy_compiler.graph.model import CatalogDocument, Concept, Provider
from policy_compiler.graph.store import Graph, load_graph
from policy_compiler.ir.common import Privilege
from policy_compiler.ir.contract import Suggestion
from policy_compiler.semantic import (
    WRITE_VERBS,
    ConceptMatch,
    SemanticIndex,
    find_write_verbs,
    stem,
    tokenize,
)

USER_EXAMPLE = "I need access to code in GitHub so that I can understand NVIDIA/OpenShell"
TOOL_DESCRIPTION = "Summarizes the architecture of a repository by reading its source files"


@pytest.fixture(scope="module")
def graph():
    return load_graph()


@pytest.fixture(scope="module")
def index(graph):
    return SemanticIndex(graph)


def concept_ids(matches):
    return [m.concept for m in matches]


# --- tokenize and stem ---------------------------------------------------------


def test_tokenize_lowercases_and_splits_on_non_alphanumerics():
    assert tokenize("Source-Code/README.md") == ["source", "code", "readme", "md"]


def test_tokenize_drops_stopwords_and_single_characters():
    assert tokenize("I need access to the code, and a b c") == ["code"]


def test_tokenize_keeps_order_and_duplicates():
    assert tokenize("code review code") == ["code", "review", "code"]


def test_tokenize_splits_underscores():
    assert tokenize("pull_requests") == ["pull", "request"]


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("issues", "issue"),
        ("requests", "request"),
        ("files", "file"),
        ("access", "access"),  # ends in "ss"
        ("class", "class"),
        ("bus", "bus"),  # too short to strip "s"
        ("reading", "read"),
        ("understanding", "understand"),
        ("using", "using"),  # "ing" only when longer than 5
        ("pushed", "push"),
        ("fixed", "fixed"),  # "ed" only when longer than 5
        ("summarizes", "summarize"),
        ("settings", "sett"),  # "s" first, then "ing": light stemming, not linguistics
    ],
)
def test_stem(word, expected):
    assert stem(word) == expected


def test_tokenize_applies_stemming():
    assert tokenize("Reading issues and pushed commits") == ["read", "issue", "push", "commit"]


# --- match ---------------------------------------------------------------------


def test_user_example_matches_source_code_read_first(index):
    matches = index.match(USER_EXAMPLE)
    assert matches[0] == ConceptMatch("source-code.read", 1.0, ["code", "understand"])


def test_keywords_are_stemmed_like_queries(index):
    # The keyword list has "issue" and "issues"; both stem to "issue".
    top = index.match("issue")[0]
    assert top.matched_terms == ["issue"]
    assert top.concept in {"issues.read", "issues.write"}


def test_match_scores_are_in_unit_interval_and_sorted(index):
    matches = index.match("open issues for bugs and read the code of pull requests")
    assert matches
    assert all(0.0 < m.score <= 1.0 for m in matches)
    keys = [(-m.score, m.concept) for m in matches]
    assert keys == sorted(keys)


def test_matched_terms_are_sorted(index):
    for m in index.match("review the pull request diff and merge it"):
        assert m.matched_terms == sorted(m.matched_terms)


def test_ties_are_broken_by_concept_id(index):
    # "code" is a keyword of both source-code concepts and nothing else's.
    matches = index.match("code")
    assert matches[0].score == matches[1].score
    assert concept_ids(matches[:2]) == ["source-code.read", "source-code.write"]


def test_match_respects_min_score_and_top_k(index):
    text = "open issues for bugs and read the code of pull requests"
    everything = index.match(text, min_score=0.0)
    assert len(index.match(text, min_score=0.0, top_k=2)) == 2
    assert all(m.score >= 0.5 for m in index.match(text, min_score=0.5))
    assert len(everything) > len(index.match(text, min_score=0.5))


def test_unknown_words_do_not_lower_the_score(index):
    plain = index.match("understand the code")
    noisy = index.match("understand the code zxqv plorbs")
    assert plain == noisy


def test_text_without_known_terms_matches_nothing(index):
    assert index.match("") == []
    assert index.match("the and of to") == []
    assert index.match("zxqv plorbs") == []


def test_provider_names_in_operation_summaries_are_not_concept_terms(index):
    # "GitHub" appears in an operation summary; naming the provider must not
    # make any concept more likely.
    assert index.match("GitHub") == []


def test_match_is_deterministic(graph, index):
    text = "open issues for bugs you find and read the code of pull requests"
    first = index.match(text, min_score=0.0)
    assert index.match(text, min_score=0.0) == first
    assert SemanticIndex(graph).match(text, min_score=0.0) == first


def test_write_concept_wins_with_a_write_verb(index):
    assert index.match("push commits that fix the code")[0].concept == "source-code.write"


def test_org_concepts_are_indexed(tmp_path):
    graph = Graph()
    graph.add_document(
        CatalogDocument(
            providers=[Provider(id="acme", name="ACME")],
            concepts=[
                Concept(id="orders.read", name="Read orders", privilege=Privilege.READ, keywords=["orders"]),
                Concept(id="invoices.read", name="Read invoices", privilege=Privilege.READ, keywords=["invoice"]),
            ],
            source="test",
        )
    )
    graph.finalize()
    matches = SemanticIndex(graph).match("look up customer orders")
    assert concept_ids(matches) == ["orders.read"]


# --- providers_mentioned -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("read code on GitHub", ["github"]),
        ("read code on github", ["github"]),
        ("see https://github.com/acme/widgets", ["github"]),
        ("see github.com.", ["github"]),
        ("raw.githubusercontent.com/acme/widgets/main/README.md", ["github"]),
        # Literal: repository names count too. The use-case front end removes
        # repository references before it asks.
        ("github.com/NVIDIA/OpenShell", ["github", "nvidia", "openshell"]),
        ("GitLab and GitHub", ["github", "gitlab"]),  # graph order, not text order
        ("gitlab.com/group/project", ["gitlab"]),
        ("models from the NVIDIA API Catalog", ["nvidia"]),
        ("use OpenAI", ["openai"]),
        ("GitHubber gitlabs", []),  # whole tokens only
        ("github.company.example", []),  # not the github.com host
        ("nothing here", []),
    ],
)
def test_providers_mentioned(index, text, expected):
    assert index.providers_mentioned(text) == expected


# --- concepts_for_operation ----------------------------------------------------


def test_concepts_for_operation_direct_realizers_first(index):
    assert index.concepts_for_operation("github.repos.get-content") == ["source-code.read", "source-code.write"]


def test_concepts_for_operation_with_alternative_requirements(index):
    assert index.concepts_for_operation("github.issues.list-comments") == [
        "issues.read",
        "code-review.read",
        "issues.write",
        "code-review.write",
    ]


def test_concepts_for_operation_puts_implied_concepts_after_direct_ones(index):
    # gitlab.api implies gitlab.read_repository, so issues.write realizes it
    # indirectly; source-code.read realizes it directly and comes first.
    concepts = index.concepts_for_operation("gitlab.repository.get-file")
    assert concepts[0] == "source-code.read"
    assert set(concepts) == {"source-code.read", "source-code.write", "issues.write"}


def test_concepts_for_operation_unknown_or_public(index):
    assert index.concepts_for_operation("nope.nothing") == []
    assert index.concepts_for_operation("github.search.code") == []


# --- suggest -------------------------------------------------------------------


def test_suggest_ranks_source_code_read_first_for_a_tool_description(index):
    suggestions = index.suggest(TOOL_DESCRIPTION)
    assert all(isinstance(s, Suggestion) for s in suggestions)
    assert suggestions[0].concept == "source-code.read"
    assert {"architecture", "read", "source"} <= set(suggestions[0].matched_terms)


def test_suggest_mirrors_match(index):
    matches = index.match(TOOL_DESCRIPTION)
    suggestions = index.suggest(TOOL_DESCRIPTION)
    assert [(s.concept, s.score, s.matched_terms) for s in suggestions] == [
        (m.concept, m.score, m.matched_terms) for m in matches
    ]


# --- write verbs ---------------------------------------------------------------


def test_write_verbs():
    assert WRITE_VERBS == frozenset(
        "write edit modify change update fix push commit create open comment reply merge delete submit file".split()
    )


@pytest.mark.parametrize(
    ("text", "asked", "negated"),
    [
        ("open issues for bugs you find", ["open"], []),
        ("read the source files", [], []),
        ("read the comments and the latest changes", [], []),  # plural nouns
        ("file a bug when a test fails", ["file"], []),
        ("filed issues", ["file"], []),
        ("read file contents", [], []),  # "file" needs an issue-like object
        ("understand this open source project", [], []),
        ("triage the open issues", [], []),  # adjective after a determiner
        ("updated the docs and created issues", ["create", "update"], []),
        ("an agent that opens pull requests and pushes commits", ["open", "push"], []),
        ("committed and submitted", ["commit", "submit"], []),
        ("replied to and modified", ["modify", "reply"], []),
        ("read code but never push", [], ["push"]),
        ("do not open or comment on issues", [], ["comment", "open"]),
        ("must not be able to delete anything", [], ["delete"]),
        ("don't merge", [], ["merge"]),
        ("WRITE the summary", ["write"], []),
    ],
)
def test_find_write_verbs(text, asked, negated):
    assert find_write_verbs(text) == (asked, negated)


def test_commit_history_is_not_a_write_request():
    from policy_compiler.semantic import find_write_verbs

    asked, _ = find_write_verbs("Read its files, README, and commit history. Never change the repository.")
    assert asked == []
    asked, _ = find_write_verbs("commit the fix and push it")
    assert "commit" in asked
