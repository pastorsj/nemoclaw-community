<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# OpenShell Policy Compiler: Design

Status: proof of concept, 2026-10-10. This document records the design and the
decisions taken while building the proof of concept, including the ones made
without a reviewer present. Each decision lists its reason so it can be revisited.

## Problem

Writing an OpenShell policy file takes a few lines of YAML. Knowing what to put in it is hard.
You have to know which providers, hosts, HTTP methods, and paths an agent needs,
which binary makes each call, and which of those calls are reads, writes, or
administrative actions. Policies written by hand tend to be too broad. In this
repository, about half of the policy files use binary globs, many path rules end
in `/**`, and some use `tls: skip`.

The goal is to produce a policy that is as narrow as possible while the agent
still works, and to show the evidence for every rule.

## Scope of the proof of concept

This proof of concept covers the *compile* step only:

```
agent definition (+ API knowledge)  →  internal contract  →  OpenShell policy + report
```

It does not observe a running agent. It does not generate test prompts, and it
does not run the agent. Those steps are later phases, and the contract leaves
room for their evidence.

## Architecture

The tool is built like a compiler: several front ends, a neutral middle, and
an OpenShell back end.

```
 FRONT ENDS (lift)            MIDDLE (internal, no external standard)        BACK END (lower)
 Agent Spec        ─┐   ┌─────────────────────────────────────────┐
 use-case text      ├──►│ CONTRACT (high level)                    │
 (later: MCP, ...)  │   │   needs · prohibitions · unknowns         │
                    │   │   at any abstraction level, with evidence │
                    │   │              │ resolve against the graph  │
 existing OpenShell │   │              ▼                            │      ┌─► policy.yaml
 policies (lift) ───┼──►│ ACCESS SET (low level)                    ├──────┤   boundary.yaml
                    │   │   concrete allow/deny tuples              │      └─► report.md / .json
                    │   └─────────────────────────────────────────┘
                    │                  ▲
 OpenAPI + Overlay ─┴─► CAPABILITY GRAPH (layered: base → org → agent)
 GitHub permission data, mined OpenShell policies, hand-curated catalog
```

### Two kinds of input

- **Knowledge inputs** describe APIs and feed the capability graph. Examples are
  OpenAPI specs, OpenAPI Overlays, GitHub's fine-grained permission data,
  existing OpenShell policies (mined with `import openshell-policies`), and
  hand-curated catalog files. An importer for OpenShell provider profiles is not
  built yet.
- **Agent inputs** describe an agent and feed the contract. Examples are Agent
  Spec files and plain use-case text.

Agent Spec links the two. When a `RemoteTool` or `ApiNode` has an
`api_spec_uri`, the compiler imports that spec into the agent layer of the graph.

### Capability graph

The graph holds these node types:

| Node | Example | Purpose |
| --- | --- | --- |
| Provider | `github` | Owner of services and capabilities. |
| Service | `github.rest` (`api.github.com:443`) | Where operations are served: origins, protocol, inspection, `allowed_ips`. |
| Operation | `github.repos.get-content` = `GET /repos/{owner}/{repo}/contents/{path}` | The unit that compiles to an L7 rule. Each operation has a privilege level and an OR-of-AND capability requirement. |
| Capability | `github.contents:read` | A permission defined by the provider, with a level. `implies` edges link levels, for example write implies read. |
| Concept | `source-code.read` | The cross-provider semantic layer. Its keywords and `realized_by` edges point to capabilities. |
| Client | `git` (`/usr/bin/git`) | A binary, plus the operations it performs implicitly. For example, `git clone` uses `POST git-upload-pack` on `github.com`. |

The graph is built from **layers**:

1. **base** ships in this repository under `catalog/base/`.
2. **org** layers are directories you pass with `--catalog`. They can live in a
   private repository and are never published.
3. **agent** layers are built automatically from specs that an agent references.

Later layers may add nodes and may attach new realizations to existing concepts.
A later layer never replaces an earlier definition. A reused ID merges only when
its identifying fields agree, and the loader warns when other fields differ.
See [the catalog format](catalog-format.md#layering-rules). Provider-owned IDs carry the provider prefix, for
example `acme.orders:read`.

### Contract (high-level internal representation)

The contract has four parts:

- **Needs.** A need has a target at any abstraction level: a `concept`, a
  `capability`, a graph `operation`, or a `raw` endpoint for something not in
  the graph. A need also records:
  - resource scope bindings, for example `{owner: NVIDIA, repo: OpenShell}`
  - the binaries that make the call
  - necessity, phase, and locus (where the call originates)
  - whether the call needs approval
  - a credential hint
  - path prefixes taken from `url_allow_list`
  - a rationale, and evidence of the form (kind, source, pointer)
- **Prohibitions.** These name operations or capabilities, or a provider together
  with a minimum privilege (for example "nothing at write level or above on
  GitHub"). Prohibitions always win over needs.
- **Unknowns.** These record what the compiler cannot see, for example opaque
  `ServerTool`s, MCP toolboxes without a tool filter, templated destinations, and
  unsupported components. The semantic layer can attach suggestions to an
  unknown, but suggestions never become grants.
- **Posture.** This sets the default effect. The proof of concept compiles
  `deny` only.

### Access set (low-level internal representation)

The access set is a list of concrete rules of the form
`(effect, group, client set, endpoint, matcher)`, plus diagnostics and widenings.
It is neutral about the output format, yet any OpenShell policy can be lifted
into it, because the matcher strings use OpenShell's glob dialect. Non-network
sections (`filesystem_policy`, `landlock`, `process`) are carried through
unchanged so that a lifted policy can be lowered back without loss.

### Resolution (contract → access set)

Each runtime need from the sandbox resolves to graph operations:

- An **operation** need resolves to that operation.
- A **capability** need resolves to every operation whose requirement is
  satisfied by the capability (including the capabilities it implies).
- A **concept** need resolves through the concept's `realized_by` capabilities.
  It can be restricted to one provider, and is reported as a widening.
- A **raw** endpoint need is matched against the graph by host, port, method, and
  path template. If it matches, the declared literal segments are kept, so the
  rule is narrower than the template. If nothing matches, the rule is emitted
  from the declared shape and a "not in catalog" diagnostic is added.

Each operation then becomes a matcher:

- Bound scope parameters become literal segments.
- Unbound single-segment parameters become `*`.
- Parameters marked as multi-segment become `**`.

Matchers that another matcher with the same method already covers are dropped.
Each client the graph knows about adds its implicit operations; for example,
`git` adds `git-upload-pack` when read access to code is granted. Prohibitions
remove conflicting allows, and they add `deny_rules` where an allow would
otherwise overlap them.

The compiler also emits a **boundary** (ceiling). The boundary contains the same
capabilities without scope pinning. It is intended as the prover's `--boundary`
input, or as the limit for auto-approving Policy Advisor proposals.

### OpenShell back end

Every generated endpoint follows these rules:

- An inspected endpoint sets `protocol` explicitly. Without a protocol, `access` and `rules` have no
  effect.
- It sets `enforcement: enforce` explicitly. The default is `audit`.
- Its `binaries` list is never empty. An empty list means "none" at runtime and
  "any" in some tooling.
- It sets `tls: skip` only when the graph marks the service as opaque, for
  example because it uses mTLS.
- It sets `allowed_ips` when the service declares private ranges.

When `--base-policy` is given, the compiler keeps the base policy's filesystem,
process, and Landlock sections and replaces only `network_policies`.

The back end can also **lift** an existing policy into the access set. Lifting
reports diagnostics for legacy or invalid values (`tls: terminate`,
`protocol: http`), empty binary lists, a missing `enforcement` (which means
audit), L4-only grants, and binary globs. Lowering a lifted policy reproduces
the original policy, after canonicalization. The test suite checks this
round trip on every policy file in this repository.

### Semantic layer

The proof of concept uses a deterministic keyword matcher instead of
embeddings. A concept's keywords and the summaries of the operations that
realize it are tokenized, then weighted with IDF. Each match is explainable:
the report lists the terms that matched. The matcher is used in three places:

1. `from-use-case` turns text such as "access code in GitHub to understand
   NVIDIA/OpenShell" into concept-level needs, with the provider and scope
   extracted.
2. `--use-case` justifies the needs declared in an Agent Spec file. A need that
   the use case does not justify is reported, and excluded with
   `--strict-use-case`.
3. It suggests capabilities for opaque tools, using their descriptions.

When a concept is realized by several providers and the text names none of
them, the compiler asks for clarification and grants nothing. It does not take
the union of all providers.

## Design decisions

| Decision | Choice | Reason |
| --- | --- | --- |
| Where the tool lives | `examples/tools/openshell-policy-compiler` | It is a developer tool, not an agent workflow (see the taxonomy). |
| Language and dependencies | Python 3.11+, PyYAML only, plus pytest for development | Matches the repository's other tools. Keeps the dependency surface small. |
| How Agent Spec is parsed | Raw YAML/JSON walker, not `pyagentspec` | Tolerates unknown plugin components, which become unknowns rather than parse failures, and spec versions newer than the SDK on PyPI (26.3.1 there, 26.4 in development). |
| Default posture | Deny | It is what OpenShell expresses well. The contract already has an `allow` slot. |
| Base catalog content | Hand-curated slices: GitHub, GitLab, NVIDIA, and OpenAI inference, plus clients | Avoids vendoring third-party datasets. The full GitHub permission data can be imported at run time with `import github-permissions`. |
| Annotating an internal API | OpenAPI Overlay plus `x-openshell-*` fields (`privilege`, `capability`, `allowed-ips`, `inspection`) | Overlay is an existing OpenAPI Initiative standard. The `x-` fields are the tool's own configuration, not a new standard. |
| Privilege when a spec says nothing | Inferred from the HTTP method and labelled `inferred` | The HTTP method is not a reliable sign of privilege. GitHub has read-level POSTs and write-level GETs, so these guesses are surfaced for review. |
| Need phases | Compile `startup`, `auth`, `runtime`, and `telemetry`; report `install` and `build` | Package-registry access should not leak into the runtime policy. |
| Ask/confirm semantics | Compiled as allow, and flagged in the report | OpenShell cannot prompt a person in the middle of a request. Policy Advisor approval is the intended follow-up. |
| Semantic layer | Keyword and IDF matching, not embeddings | Deterministic and testable offline. Embeddings can be added behind the same interface. |
| Declared methods | A catalogued `*` operation never widens the method an agent declares | Policies mined from hand-written files often use `*`. |
| GitHub's flat permission data | Require every listed permission when GitHub marks additional permissions, and classify at the highest level | The flat data cannot tell "either" from "both". Under-granting is visible in the report; escalation is not. |
| Prohibitions on broad or uncatalogued grants | A provider prohibition removes connection-level grants on that provider's hosts, narrows `*` methods to `GET`, and removes uncatalogued rules at or above the forbidden level | Templated destinations produce `* /prefix/**` rules that catalogued deny rules alone cannot contain. |
| Opaque services | One connection-level grant plus a recorded widening | L7 rules under `tls: skip` would be ignored by OpenShell, so emitting them would mislead the reader. |
| Concepts realized by several providers | Grant nothing and ask for a provider | Taking the union of every provider is the opposite of least privilege. |
| Write intent in use cases | Only requested, non-negated write verbs that name an object keyword justify write access | Read-only phrasings such as "list open issues" must not grant writes. Under-granting is reported with a rephrasing hint. |
| Special-purpose clients | Binaries with implicit uses, such as `git`, receive only those uses, pinned to the declared repository | Prevents `git` from cloning any repository when the agent declares one. |
| Boundary | The boundary always contains the policy | Tested on every example and on the review's probe contracts. |
| Lowering | `lower` never emits a policy broader than the access set; it drops what it cannot express and reports an error | A policy file that silently widens access is worse than one that is rejected. |

## Known gaps (by design in the proof of concept)

- No observation run. Egress that is not declared (runtime telemetry, opaque
  tools) shows up only as an unknown.
- GitHub's flat permission data cannot tell "either permission" from "both
  permissions". When GitHub marks additional permissions, the importer fails
  safe: it requires all of the listed permissions and classifies the operation
  at the highest level. This can under-grant. The curated base catalog keeps
  the verified "either" requirements, such as the one for issue comments.
- Query, header, and body constraints are not generated.
- The prover is not installed locally, so containment is not checked.
  `boundary.yaml` is written for use with `openshell-prover check`.
- AWS JSON-protocol services, GraphQL, and `$batch` endpoints are not in the
  base catalog.
