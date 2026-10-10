<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# OpenShell Policy Compiler: Proof-of-Concept Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task.

**Goal:** Compile an Agent Spec agent definition, or a plain use-case statement,
into a least-privilege OpenShell network policy. The compiler also writes a
boundary policy and a report that names the evidence for every rule.

**Architecture:** Front ends turn agent inputs into a contract. Importers turn
API knowledge into a layered capability graph. The resolver uses the graph to
turn the contract into an access set. The OpenShell back end lowers the access
set to policy YAML, and can also lift existing policies back into an access
set. See `docs/design.md`.

**Tech stack:** Python 3.11+, PyYAML, pytest, uv.

**Already done (commit 76e8b9ac):**

- `ir/common.py`, `ir/contract.py`, `ir/access.py`, and `graph/model.py`: the
  data model.
- `paths.py`: template and glob matching.
- `catalog/base/*.yaml` and `docs/catalog-format.md`.

**Conventions for every task:**

- Run commands from `examples/tools/openshell-policy-compiler`. Use
  `uv run pytest`.
- Every new `.py`, `.yaml`, `.yml`, `.toml`, and `.sh` file starts with the two
  SPDX lines used in the existing files.
- Write tests first (TDD), and put them in the task's own test file.
- Do not edit files that another task owns. Do not edit `ir/`, `graph/model.py`,
  or `paths.py`. If you need a change there, report it.
- Do not commit. The coordinator reviews and commits.
- Treat the cloned repositories under the scratchpad research directory as
  read-only data. Never execute code from them.

---

## Wave 1 (parallel)

### Task A: Layered catalog store (`graph/store.py`)

**Files:** create `src/policy_compiler/graph/store.py` and
`tests/test_store.py`.

**Interface:**

```python
BASE_CATALOG_DIR: Path   # <tool>/catalog/base; env POLICY_COMPILER_BASE_CATALOG overrides
class CatalogError(Exception): ...
def parse_catalog_document(data: dict, *, source: str, layer: Layer) -> CatalogDocument
def dump_catalog_document(doc: CatalogDocument) -> dict      # inverse of parse; round-trips
def load_catalog_file(path: Path, *, layer: Layer) -> CatalogDocument
class Graph:
    providers / services / operations / capabilities / concepts / clients: dict[str, node]
    def add_document(self, doc: CatalogDocument) -> None
    def finalize(self) -> list[Diagnostic]
    def services_for(self, host: str, port: int) -> list[Service]
    def operations_of(self, service_id: str) -> list[Operation]
    def capability_closure(self, ids) -> set[str]
    def operations_enabled_by(self, capability_ids, services: set[str] | None = None) -> list[Operation]
    def capabilities_for_concept(self, concept_id: str, provider: str | None = None) -> list[str]
    def concepts_for_capability(self, capability_id: str) -> list[str]
    def match_rest(self, host: str, port: int, method: str, path_template: str) -> list[tuple[Operation, str]]
    def clients_for_path(self, binary_path: str) -> list[Client]
def load_graph(catalog_dirs: list[Path] = (), *, include_base: bool = True,
               extra_documents: list[CatalogDocument] = ()) -> Graph
```

**Behavior:**

- Parse every field in `docs/catalog-format.md`, applying the defaults it lists.
- Each node gets `Evidence(CURATED, "catalog:<relative file>", "<list>[i]")` and
  keeps any explicit `evidence` the file provides.
- `load_graph` order: the base directory (when `include_base`), then each
  directory in `catalog_dirs`, then `extra_documents` (the agent layer). Within a
  directory, files load in sorted order. Base files get `Layer.BASE`, catalog
  directories get `Layer.ORG`, and extra documents keep their own layer.
- **Duplicate IDs:** if the existing node has the same semantic fields
  (ignoring layer and evidence), merge by appending the new evidence. Otherwise
  raise `CatalogError` naming both sources. Semantic fields by node type:
  - capabilities: `provider`, `level`;
  - operations: `service`, `selector`, `privilege`;
  - services: `provider`, `origins`;
  - providers: `name`.
- **Prefix rule:** service, capability, and operation IDs must start with
  `<provider>.`. An operation's provider is its service's provider. Raise
  `CatalogError` on a violation.
- **`finalize()`:**
  - Apply `concept_links` by appending to `realized_by` and removing duplicates.
  - Check that references resolve: an operation's service and `requires`
    capabilities, a capability's `implies`, a concept's `realized_by`, and each
    client's `uses` operations and `when_capability`. Raise `CatalogError`
    listing every dangling reference.
  - Return info diagnostics, such as operations with empty `requires`.
- **`operations_enabled_by`:**
  - Compute the closure (following `implies` transitively).
  - An operation is enabled when any of its `requires` AND-lists is a subset of
    the closure.
  - Operations with `requires == []` are never returned by this method; they
    are only reachable by direct reference.
- **`concepts_for_capability(c)`:** returns the concepts that list `c` in
  `realized_by`, or that list a capability whose closure contains `c`. So a
  concept realized by `github.contents:write` also covers
  `github.contents:read`.
- **`match_rest(host, port, method, template)`:**
  - Look at the services whose origins match the host (case-insensitive, also
    through `paths.host_matches` when an origin host is a pattern) and the port.
  - Keep operations whose selector is a `RestSelector` with an equal method, or
    any method when the declared method is `*`.
  - Call `paths.align(template, op.path, set(op.multi_segment))`. Keep the
    operations where the result is not `None`, paired with the aligned glob.
- **`clients_for_path(p)`:** returns the clients whose `paths` contain `p`
  exactly.

**Tests:**

- Load the real base catalog and finalize it without errors.
- A duplicate ID with a conflicting definition raises; an identical duplicate
  merges its evidence.
- A prefix violation raises.
- A dangling reference raises.
- `concept_links` from `gitlab.yaml` are applied to `source-code.read`.
- `operations_enabled_by({"github.contents:write"})` includes
  `github.repos.get-content` (through `implies`) and excludes
  `github.search.code`.
- DNF: `operations_enabled_by({"github.pull_requests:write"})` includes
  `github.issues.create-comment`.
- `match_rest("api.github.com", 443, "GET", "/repos/NVIDIA/OpenShell/contents/{path}")`
  returns `github.repos.get-content` with the glob
  `/repos/NVIDIA/OpenShell/contents/**`.
- `match_rest` on an unknown host returns `[]`.
- `dump_catalog_document(parse(...))` round-trips `github.yaml`.

### Task B: OpenShell back end (`backends/openshell.py`)

**Files:** create `src/policy_compiler/backends/openshell.py` and
`tests/test_openshell_backend.py`.

**Read first (read-only):** the OpenShell clone at
`<scratchpad>/research/OpenShell`, especially:

- `docs/how-it-works/policies/schema.mdx` and `network-rules.mdx`, including
  the MCP, GraphQL, JSON-RPC, and deny-rule YAML forms;
- `crates/openshell-policy-schema/src/lib.rs`.

Also read `<scratchpad>/research/notes/04-inventory-knowledge-policy-formats.md`
for the field-by-field semantics.

**Interface:**

```python
def load_policy_file(path: Path) -> dict          # YAML or JSON
def lift(policy: dict, *, source: str = "") -> AccessSet
def lower(access: AccessSet, *, base_policy: dict | None = None, normalize: bool = True) -> dict
def canonicalize(policy: dict) -> dict
def dump_policy_yaml(policy: dict, *, header: list[str] = ()) -> str
```

**`lower` rules:**

- The output always has `version: 1` first.
- Non-network sections come from `access.extras`. When `base_policy` is given,
  every key except `network_policies` and `network_middlewares` comes from the
  base policy.
- Policy entries are keyed by `rule.group`, in `access.group_order` first and
  then in order of first appearance.
- All rules in one group must have the same `ClientSet`. If they differ, split
  the group as `<group>_2`, and so on.
- Each entry has `name` (`rule.name` or the group), `endpoints`, and `binaries`
  (`[{path: p}]`). Raise `ValueError` when a client set's mode is `none` or
  `any` and `normalize=True`.
- Endpoints are merged by `EndpointKey`, in order of first appearance. Each
  endpoint has:
  - `host`, omitted when empty;
  - `port`, or `ports` when there are several;
  - `path`, only when set;
  - `protocol`, omitted for L4;
  - `tls: skip` when the endpoint is opaque;
  - `enforcement`, always emitted, including `audit`;
  - `allowed_ips`, when set;
  - `access`, when a rule carries `access_preset`;
  - `rules: [{allow: {...}}]` for allow matchers;
  - `deny_rules: [{...}]` for deny matchers;
  - `credential_binding: {provider: X}`, when set;
  - `rule.endpoint_extras`, merged last.
- Matcher keys are emitted only when set: `method`, `path`, `query` (as a
  mapping), `tool`, `operation_type`, `operation_name`, `fields`, `command`.
- Duplicate matchers are removed, keeping the first.
- **`normalize=True`:**
  - Drop the legacy `tls: terminate`.
  - Map `tls: passthrough` to `skip`.
  - Never emit `binaries: []`.
- **`normalize=False`:** reproduce what was authored, using the lifted
  extras.

**`lift` rules:**

- Accept a full policy, a NemoClaw preset (`{preset: ..., network_policies: ...}`),
  or a fragment containing only `network_policies`.
- Each endpoint and matcher becomes an `AccessRule`:
  - one rule per allow matcher, one per deny matcher;
  - one endpoint-level rule when the endpoint has `access` or no L7 rules.
- `reasons` hold `"<source>#network_policies.<key>.endpoints[i]"`.
- Keep the authored key order in `access.group_order`, and the authored
  `name`.
- Any unknown endpoint field is kept in `endpoint_extras`.
- The original `tls` value is kept in `endpoint_extras` when it isn't `""` or
  `skip`.
- **Diagnostics** (code, level):

| Code | Level |
| --- | --- |
| `legacy-tls-value` | warning |
| `invalid-protocol` | error |
| `empty-binaries` | warning |
| `missing-enforcement` | warning: OpenShell defaults to audit, so L7 rules are not enforced |
| `l4-only` | warning |
| `binary-glob` | warning |
| `tls-skip` | warning |
| `access-full` | warning |
| `path-wildcard-all` | warning, for a `/**` or `**` allow path |
| `wildcard-host` | info |
| `placeholder` | info, for `__X__`, `{x}`, or `*_PLACEHOLDER` values |

**`canonicalize`:**

- Sort mapping keys recursively.
- Expand `port` into `ports`.
- Default `name` to the key.
- Fold a `tool` field into `params.name` only if the docs say they are
  equivalent; otherwise leave it.
- Remove empty lists and mappings.

**Tests:**

1. Golden lower: a hand-built `AccessSet` for GitHub read access becomes the
   exact expected dict (`protocol: rest`, `enforcement: enforce`, rules, and
   binaries).
2. Groups with different client sets are split.
3. `base_policy` keeps `filesystem_policy` and replaces `network_policies`.
4. **Round trip over every policy file in this repository:**
   - Glob `../../**/policy*.yaml`, `../../**/policies/*.yaml`, and
     `../../**/policies/*.json` under `nemoclaw-community/examples`, excluding
     `.venv` and `node_modules`.
   - Check that `canonicalize(lower(lift(p), normalize=False)) == canonicalize(p)`
     for the network sections.
   - Templated files must round-trip too.
5. `lift` reports `legacy-tls-value` for
   `recipes/nvidia/agentic-ai-learning-path/policy.yaml` and `invalid-protocol`
   for `recipes/community/axe-a11y-browser-auditor/policy.yaml`.
6. `lower` raises `ValueError` on empty clients when `normalize=True`.

### Task C: Agent Spec front end (`frontends/agentspec.py`)

**Files:** create `src/policy_compiler/frontends/agentspec.py`,
`tests/test_agentspec_frontend.py`, and `tests/fixtures/agentspec/*.yaml`.

**Read first:**

- `<scratchpad>/research/notes/05-inventory-agent-side-formats.md`, section 1;
- `<scratchpad>/research/agent-spec/docs/pyagentspec/source/agentspec_config_examples/`;
- the component sources under `<scratchpad>/research/agent-spec/pyagentspec/src/pyagentspec/`.

**Interface:**

```python
def load_agentspec(path: Path, *, reference_files: list[Path] = ()) -> dict
def contract_from_agentspec(path: Path, *, default_clients: list[str],
                            reference_files: list[Path] = ()) -> Contract
```

**Behavior:**

- **Parsing:**
  - Parse `.yaml`, `.yml`, and `.json`.
  - Resolve `{"$component_ref": id}` using `$referenced_components` from the
    file and from the reference files. Resolution is recursive.
  - An unresolved reference becomes `Unknown(UNRESOLVED_REFERENCE)`.
- **Walking:** visit every dict that has `component_type`. Track a JSONPath-like
  pointer such as `$.tools[0]` or `$.nodes[2].tool`.
- **Need IDs:** `need-NN-<slug(component name)>`, numbered in walk order. Every
  need gets `Evidence(DECLARED, "agentspec:<file name>", pointer)` and
  `subject` set to the component name.
- **`Contract`:**
  - `agent` is the root `name`.
  - `use_case` is the root `description` plus `system_prompt`, when present.
  - `sources` is `[str(path)]`.
  - `default_clients` comes from the caller.
  - `referenced_specs` holds `api_spec_uri` values. A relative path resolves
    against the spec file's directory; URLs are kept as they are.

**Component mapping:**

- **`RemoteTool` and `ApiNode`:**
  - Parse `url` after `paths.normalize_agentspec_template`. A placeholder in the
    scheme, host, or port is a templated destination.
    - With a `url_allow_list`: emit one need per allow-list entry. Each need
      is a raw endpoint with that host and port, the method, and the path
      prefix + `/**`, and its `path_prefixes` are set.
    - Without one: add `Unknown(OPEN_DESTINATION)` and no need.
  - Otherwise emit a raw-endpoint need: scheme, host, port (default 443 for
    https, 80 for http), the upper-cased method, the normalized path template,
    and `Protocol.REST`.
  - A placeholder in `http_method` becomes `*`, with a rationale note.
  - `url_allow_list` entries become `path_prefixes`, but only for entries whose
    scheme and host match the URL.
  - A `requires_confirmation` of `true` becomes `Approval.CONFIRM`.
  - An `Authorization` header, or any key in `sensitive_headers`, becomes
    `Credential("header:<name>")`.
  - `rationale` is the description, or "RemoteTool <name>".
- **LLM configs:**
  - `OpenAiCompatibleConfig`, `VllmConfig`, `OllamaConfig`: the URL becomes a
    raw endpoint.
    - A URL without a scheme gets `http://`, which is what the adapters do.
    - The path is the URL path joined with `/chat/completions`; use
      `/responses` when `api_type == "responses"`. Adapters force a `/v1`
      prefix, so add `/v1` when the path lacks it.
    - The method is POST.
  - `OpenAiConfig`: `api.openai.com:443` with `POST /v1/chat/completions` (or
    `/v1/responses`). Add a rationale note that the adapters ignore `url`.
  - `OciGenAiConfig`: the `client_config.service_endpoint` host, at endpoint
    level (method and path `None`).
  - `GeminiConfig`: `generativelanguage.googleapis.com`, endpoint level,
    labelled `EvidenceKind.INFERRED`.
  - An unknown `*Config` that has `llm_config` semantics:
    `Unknown(UNSUPPORTED_COMPONENT)`.
- **MCP:**
  - `MCPToolBox` or `MCPTool` with `SSETransport` or `StreamableHTTPTransport`
    (or their mTLS variants): a raw endpoint with `Protocol.MCP`, the URL's host
    and port, the URL path, and `mcp_tools` from `tool_filter` (strings, or
    `MCPToolSpec.name`) or from the `MCPTool` name.
  - No `tool_filter` on a toolbox: add `Unknown(OPEN_TOOLSET)` as well.
  - `StdioTransport`: `Unknown(OPAQUE_IMPL)`, recording the command.
- **`A2AAgent`:** a raw endpoint at `agent_url`'s host and port, with
  `POST <path or />`.
- **Opaque and non-network tools:**
  - `ServerTool`, `BuiltinTool`: `Unknown(OPAQUE_IMPL)` with the tool's
    description.
  - `ClientTool`: a note that it runs outside the sandbox (locus `CLIENT`). It
    produces no need.
- **Datastores:**
  - Postgres datastores (any `component_type` containing `Postgres`): parse
    `connection_config.url` or `url`, and emit a raw endpoint with
    `Protocol.TCP`, host and port (default 5432), and method and path `None`.
  - Oracle datastores: `Unknown(UNRESOLVED_REFERENCE)`, because the DSN is
    sensitive.
- **Other types:**
  - Any other `component_type` that is not structural:
    `Unknown(UNSUPPORTED_COMPONENT)`. Structural types are Agent, Flow, the
    nodes, ToolBox, ManagerWorkers, Swarm, SpecializedAgent, transports, and
    Property, among others.
  - Recurse through every dict and list value either way.

**Tests:** write fixtures covering each mapping. They should include a GitHub
agent with `RemoteTool` operations, `url_allow_list`, a `ServerTool`,
`OpenAiCompatibleConfig` pointing at `https://integrate.api.nvidia.com/v1`, an
MCP toolbox with and without a filter, and a disaggregated `$component_ref`.
Assert on the needs, unknowns, pointers, and IDs.

### Task D: Graph importers (`graph/importers/`)

**Files:**

- `graph/importers/openapi.py` and `graph/importers/overlay.py`;
- `graph/importers/github_permissions.py`;
- `graph/importers/openshell_policies.py`;
- tests: `tests/test_import_openapi.py`, `tests/test_import_github.py`,
  `tests/test_import_openshell_policies.py`, and fixtures.

**Interface:**

```python
# overlay.py
def apply_overlay(spec: dict, overlay: dict) -> dict  # OpenAPI Overlay 1.0.0; returns a new dict
# openapi.py
def load_spec_file(path: Path) -> dict
def import_openapi(spec: dict, *, provider_id: str, source: str, layer: Layer,
                   overlay: dict | None = None, server_url: str | None = None,
                   provider_name: str | None = None) -> tuple[CatalogDocument, list[Diagnostic]]
# github_permissions.py
def import_github_permissions(data: dict, *, source: str, layer: Layer) -> CatalogDocument
# openshell_policies.py
def import_openshell_policies(policies: list[tuple[str, dict]], *, known: Graph | None,
                              layer: Layer) -> tuple[CatalogDocument, dict]  # doc + usage index
```

**OpenAPI behavior:**

- **Overlay:**
  - Apply the overlay first.
  - Read the Overlay 1.0.0 spec (https://spec.openapis.org/overlay/v1.0.0.html)
    for the `update` and `remove` merge semantics.
  - Implement this JSONPath subset: `$`, `.name`, `['name']`, `["name"]`, `.*`,
    `[*]`, and the filter `[?(@.key == 'value')]`.
- **Services:**
  - `servers[0].url`, or `server_url`, gives the origin (scheme, host, port) and
    a base path. Server variables use their defaults.
  - A relative server URL with no `server_url` is a diagnostic error, and no
    service is created.
  - Service ID: `<provider>.<host slug>`.
  - `x-openshell-allowed-ips`, on the root or on a server, sets `allowed_ips`.
  - `x-openshell-inspection` (`l7` or `opaque`) sets `inspection`.
- **Operations:**
  - One operation per path and method. ID: `<provider>.<operationId slug>`, or
    `<provider>.<method>-<path slug>`.
  - The path is the base path plus the OpenAPI path.
  - Multi-segment parameters: `x-openshell-multi-segment`, or parameters with
    `x-multi-segment: true` or `allowReserved: true`.
  - `summary` comes from `summary`, or else `description`. `tags` come from
    `tags`.
- **Capabilities and privilege, in priority order:**
  1. The operation's `x-openshell-capability` (string or list) and
     `x-openshell-privilege` (documented, from the overlay or the API owner).
  2. OAuth2 or OpenID Connect scopes in the effective `security`, from the
     operation or the root. Each requirement object becomes one AND-list
     (`<provider>.<scope>`), and the array is the OR. A scope's level comes from
     `x-openshell-scope-levels` at the root, if present (documented). Otherwise
     it is inferred from the scope name: read, readonly, or view means read;
     admin, delete, or manage means admin; anything else means write.
  3. Neither: privilege comes from `ir.common.method_privilege` (INFERRED,
     confidence 0.5), and the capability is `<provider>.<first tag or "api">:<level>`
     (INFERRED).
  
  The operation's privilege is the minimum over the OR-sets of the maximum
  capability level in each AND-set. Without scopes, it is the method guess.
  Inferred levels get a diagnostic info summarizing the counts.
- **Concepts:** `x-openshell-concepts` on an operation or the root (concept
  IDs) becomes `concept_links` from that operation's capabilities.

**GitHub permissions** (format: `{perm: {title, permissions: [{verb, requestPath, access, category, slug, additional-permissions}]}}`):

- Capabilities: `github.<perm>:<access>`. Write implies read, and admin implies
  write when those exist.
- Operations:
  - Deduplicate by verb and path. ID: `github.<category>.<slug>`.
  - Service: `github.rest`. The service and provider are also emitted, and are
    identical to the base catalog so they merge.
  - `requires` lists one `[cap]` per permission entry.
  - The privilege is the minimum access across entries.
  - A parameter named `path` is multi-segment.
- Evidence: `DOCUMENTED`, source `github-docs:<file name>`.

**OpenShell policies (mining existing policies into the graph):**

- Lift each policy with the Task B back end. If Task B is not ready, parse the
  minimal fields yourself in a private helper, then switch.
- For every endpoint host:
  - **Not served by `known`:** emit provider `mined`, a service
    `mined.<host slug>` with origins (host and ports) and `allowed_ips`, and
    operations `mined.<host slug>.<method>-<path slug>` for each REST allow
    matcher. The privilege comes from the method or the preset, and is
    INFERRED. `requires` is `[]`. Evidence: LIFTED, pointing at the source.
  - **Served by `known`:** emit nothing. Record usage instead.
- Clients: `mined.client.<basename>` for each exact binary path.
- The usage index is
  `{host: {"policies": [sources], "methods": sorted, "binaries": sorted}}`.

**Tests:**

- OpenAPI:
  - A fixture with scopes; one without scopes, using tags; one with an overlay
    that marks `POST /orders/search` as read and sets `allowed_ips`.
  - Relative server URL handling.
  - An `operationId` slug.
- GitHub: a small fixture in the real format, covering the read/write implies
  edges and DNF from two permissions.
- Mining: three small policies.
  - A host known to the base graph produces only usage.
  - An unknown host produces services and operations.

### Task E: Semantic layer and use-case front end

**Files:** create `src/policy_compiler/semantic.py`,
`src/policy_compiler/frontends/usecase.py`, `tests/test_semantic.py`, and
`tests/test_usecase_frontend.py`.

**Interface:**

```python
# semantic.py
@dataclass class ConceptMatch: concept: str; score: float; matched_terms: list[str]
def tokenize(text: str) -> list[str]
class SemanticIndex:
    def __init__(self, graph: Graph) -> None
    def match(self, text: str, *, min_score: float = 0.15, top_k: int = 8) -> list[ConceptMatch]
    def providers_mentioned(self, text: str) -> list[str]
    def concepts_for_operation(self, operation_id: str) -> list[str]
    def suggest(self, text: str) -> list[Suggestion]
WRITE_VERBS: frozenset[str]
# frontends/usecase.py
def contract_from_use_case(text: str, graph: Graph, *, default_clients: list[str],
                           inference: str = "local", agent_name: str = "use-case") -> Contract
```

**Semantic behavior:**

- **Tokenizing:** lowercase; split on non-alphanumeric characters; drop
  stopwords (a short English list). Apply light stemming: strip a trailing `s`
  when the token is longer than 3 characters and does not end in `ss`, then
  `ing` and `ed` when the token is longer than 5. The same stemming applies to
  keywords.
- **Indexing:** a concept's term bag is its keywords (weight 2), plus the tokens
  of its name and description, plus the tokens of the summaries and tags of the
  operations its capabilities enable (weight 1). Use the graph's
  `operations_enabled_by(capabilities_for_concept(c))`. Compute IDF across
  concepts.
- **Scoring:** the sum of matched term weight × IDF, divided by the sum over the
  query's tokens that appear in any bag (so 0 to 1), times a coverage factor.
  Keep it simple and deterministic, and document the formula in the docstring.
  `matched_terms` are sorted.
- **Providers:** `providers_mentioned` finds provider IDs and names as whole
  tokens, plus host names (for example `github.com`).
- **`concepts_for_operation`:** uses the operation's requirement capabilities
  and `graph.concepts_for_capability`.

**Use-case front end:**

- Match concepts.
- **Privilege:** keep write-level concepts only if the text contains a write
  verb (write, edit, modify, change, update, fix, push, commit, create, open,
  comment, reply, merge, delete, submit, file). Otherwise drop them and add a
  note.
- **Provider:** use the mentioned providers.
  - If a concept is realized by capabilities from several providers and the
    text mentions none of them, add `Unknown(AMBIGUOUS)` with the candidate
    providers, and no need.
  - If exactly one provider realizes the concept, use it.
- **Scope:**
  - From a `github.com/<owner>/<repo>` URL or a bare `<owner>/<repo>` token,
    for GitHub: `{owner, repo}`.
  - For GitLab: `{id: "<group>%2F<project>", namespace, project}`.
- **Needs:** a concept target with the provider and scope, rationale
  `use case: matched <terms>`, and `Evidence(USER, "use-case", note=text)`.
- **Inference need:** `local` gives capability `openshell.inference:invoke`;
  `nvidia` and `openai` give their capabilities; `none` adds nothing.
- `use_case` is the text.

**Tests:**

- The user's example, "I need access to code in GitHub so that I can understand
  NVIDIA/OpenShell", gives `source-code.read` on `github` with scope
  `{owner: NVIDIA, repo: OpenShell}`, plus inference, and no write concepts.
- "access to code" alone gives an `AMBIGUOUS` unknown that lists github and
  gitlab, and no source-code need.
- "open issues for bugs you find in NVIDIA/OpenShell on GitHub" includes
  `issues.write`.
- Tokenizer and stemming unit tests.
- Scores are deterministic.

---

## Wave 2: Resolver, analysis, report, and CLI (coordinator)

- `resolve.py`: contract plus graph to access set and boundary (see the design).
  Covers prohibitions, client uses, subsumption, use-case justification, and
  inference.local routing.
- `analysis.py`: the graph operations an access set permits (for `explain`),
  plus breadth metrics.
- `report.py`: Markdown and JSON reports.
- `cli.py`: `compile`, `from-use-case`, `explain`, `lift`, `import openapi`,
  `import github-permissions`, `import openshell-policies`, and `catalog check`.

## Wave 3: Examples, live check, and documentation (coordinator)

- `examples/github-repo-explainer/`: an Agent Spec agent, golden output, and
  the use-case variant.
- `examples/internal-orders-api/`: OpenAPI, an Overlay, an org catalog, and an
  Agent Spec agent. This demonstrates adding an API that only your organization
  can reach.
- `scripts/live_check.py`: create a throwaway sandbox on the local gateway,
  apply the compiled policy, probe allowed and denied requests, and delete the
  sandbox.
- `README.md` following the repository template, plus the catalog index.

## Wave 4: Review and finish

- Code review subagent, then fixes.
- Full test suite, license check in a clean worktree, and `git diff --check`.
- Push the branch to the `pastorsj` fork. Do not open a pull request upstream.
