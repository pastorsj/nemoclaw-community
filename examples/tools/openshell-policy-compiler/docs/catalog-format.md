<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Catalog File Format

A catalog is a directory of YAML files. Each file adds nodes to the capability
graph. The base catalog ships in `catalog/base/`. Your own catalogs are
directories you pass with `--catalog DIR`, one flag per directory, in order.
They form the org layer.

Every top-level key is optional except `catalog_version`.

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
        scheme: https             # default https
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
    method: GET                   # REST operations: method + path
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
- Every ID must be unique across all layers. If a later layer reuses an ID, the
  loader reports an error instead of replacing the earlier definition.
- Services, capabilities, and operations must use their provider's ID as a
  prefix. Concepts form a shared vocabulary. Use `concept_links` to add
  realizations to a concept that an earlier layer defines.
- References must resolve after all layers load. This covers an operation's
  `service` and `requires`, a concept's `realized_by`, and a client's `uses`.

## Evidence

Every node gets a `curated` evidence record that points at its file and list
index. To record a different source, add an `evidence` list to the node:

```yaml
evidence:
  - kind: documented              # declared | documented | curated | inferred | observed | user | lifted
    source: https://docs.github.com/en/rest/repos/contents
    note: Permissions listed on the endpoint page.
```
