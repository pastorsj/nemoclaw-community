<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Catalog File Format

A catalog is a directory of YAML files. Each file adds nodes to the capability
graph. The base catalog ships in `catalog/base/`. Your own catalogs are
directories you pass with `--catalog DIR`, one flag per directory, in order.
They form the org layer.

Every top-level key is optional except `catalog_version`. The loader is
strict: an unknown key, or a key repeated in the same mapping, is an error, so
a typo such as `require:` cannot silently make an operation public.

```yaml
catalog_version: 1

providers:
  - id: acme                      # lower-case; prefixes every ID the provider owns
    name: ACME Corp
    description: Internal services.

services:
  - id: acme.orders               # <provider>.<name>
    provider: acme
    name: Orders API
    protocol: rest                # rest | graphql | mcp | json-rpc | websocket | sql | tcp | l4 (default rest)
    inspection: l7                # l7 | opaque (default l7; opaque compiles to tls: skip)
    origins:
      - host: orders.internal.example
        port: 443                 # default 443
        scheme: https             # https | http (default https)
    allowed_ips: [10.20.0.0/16]   # optional; needed for private addresses
    description: ...

capabilities:
  - id: acme.orders:read          # <provider>.<name>:<level>
    provider: acme
    name: Orders (read)
    level: read                   # read | write | admin
    implies: []                   # capability IDs granted along with this one
    description: ...

operations:
  - id: acme.orders.list          # <provider>.<...>
    service: acme.orders
    method: GET                   # REST operations: method + path; an HTTP method or *
    path: /v1/orders/{order_id}   # template; {name} is one segment
    multi_segment: []             # parameters that may contain "/"
    privilege: read               # read | write | admin | unknown
    requires: [[acme.orders:read]]  # OR of AND lists; [] means no capability needed
    summary: List orders
    tags: [orders]
  - id: acme.tools.search
    service: acme.tools
    mcp_tool: search              # MCP operations: tool name instead of method + path
    privilege: read

concepts:
  - id: source-code.read          # shared vocabulary; not provider-prefixed
    name: Read source code
    privilege: read
    keywords: [code, source, repository, file, clone]
    realized_by: [github.contents:read]

concept_links:                    # attach your capabilities to an existing concept
  - concept: source-code.read
    realized_by: [acme.code:read]

clients:
  - id: git
    paths: [/usr/bin/git]         # real executable paths; OpenShell resolves symlinks
    description: Git command-line client.
    bypasses_l7: false
    can_exfiltrate: true
    uses:                         # operations the binary performs implicitly
      - when_capability: [github.contents:read]
        operations: [github.git.info-refs, github.git.upload-pack]
```

## Layering rules

- Files load in sorted order within a directory, and directories load in the
  order given: the base catalog first, then each `--catalog`, then the agent
  layer.
- A later definition never replaces an earlier one. When a file reuses an ID,
  in the same layer or a later one:
  - **Providers, services, capabilities, and operations** merge if their
    identifying fields agree: a provider's `name`; a service's `provider` and
    `origins`; a capability's `provider` and `level`; an operation's `service`,
    method and path (or MCP tool), and `privilege`. The node keeps its first
    definition and gains the later file's evidence. If those fields disagree,
    the loader reports an error that names both files.
  - If any other field differs, for example an operation's `requires` or a
    capability's `implies`, the first definition still wins. Wording fields
    (`name`, `description`, `summary`, `tags`) are not compared. The
    loader reports an `ignored-redefinition` warning that names the node, the
    fields, and both files. The later file's evidence is kept with the note
    `redefinition ignored for: <fields>`.
  - **Concepts and clients** never merge. Any reused ID is an error.
- Services, capabilities, and operations must use their provider's ID as a
  prefix. Concepts form a shared vocabulary. Use `concept_links` to add
  realizations to a concept that an earlier layer defines.
- References must resolve after all layers load. This covers a service's or
  capability's `provider`, a capability's `implies`, an operation's `service`
  and `requires`, a concept's `realized_by`, a concept link's `concept`, and a
  client's `uses` (both `operations` and `when_capability`).
- The agent layer comes from specs that an agent definition references, so it
  is less trusted. An agent-layer operation that attaches to a base or org
  service, or that only base or org capabilities gate, changes what an
  existing grant allows. The loader reports an `agent-layer-extends` warning
  for it.

## Evidence

Every node gets a `curated` evidence record that points at its file and list
index, for example source `catalog:base/github.yaml` with pointer
`operations[3]`. Files in the base catalog are labelled `base/<file>`. Files in
the n-th `--catalog` directory are labelled `org<n>:<directory name>/<file>`, so
two directories with the same name stay distinct. To record a different source,
add an `evidence` list to the node:

```yaml
evidence:
  - kind: documented              # declared | documented | curated | inferred | observed | user | lifted
    source: https://docs.github.com/en/rest/repos/contents
    note: Permissions listed on the endpoint page.
```
