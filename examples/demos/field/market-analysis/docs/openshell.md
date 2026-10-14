<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# OpenShell

The Hermes agent runs in an [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) 0.1.2 sandbox, built only
from official artifacts: the gateway, supervisor and sandbox images pinned by digest, and the CLI from the
release tarball checked by SHA-256. The release is pinned in one file, `infra/openshell/versions.env`.
[`infra/openshell/README.md`](../infra/openshell/README.md) has the Compose contract and the exact commands;
this page explains how the pieces fit and what they guarantee.

```text
api ──▶ hermes-gateway (openshell forward service) ──gRPC/mTLS──▶ openshell gateway :18080
                                                                    │ Docker driver
                                                                    ▼
                             supervisor container (host network) ◀─▶ sandbox (no network)
                                        │                            Hermes on 127.0.0.1:8642
                                        ▼
         host.openshell.internal = 127.0.0.1 ─▶ Switchyard :4000, API :8000, MCP :8120/:3010/:3003, Phoenix :6006
```

- The sandbox has no network interface. Every connection Hermes opens is checked against the policy and made
  by the host-networked supervisor.
- The gateway's `grpc_endpoint` is the IP literal `127.0.0.1`, so the supervisor maps `host.openshell.internal`
  to host loopback. That is why every service the sandbox calls is published on `127.0.0.1:<port>`.
- The sandbox has no inbound ports either. The API reaches Hermes through `openshell forward service`, run by
  the `hermes-gateway` container.

## The sandbox image

`agent/Dockerfile` builds `market-demo/hermes-sandbox:local` from `nousresearch/hermes-agent:v2026.9.24`
(Hermes 0.21.5, pinned by digest):

| Path in the image | What |
|---|---|
| `/opt/hermes` | Hermes with the four patches in `agent/patches/` applied (read-only) |
| `/etc/openshell/policy.yaml` | The sandbox policy, `agent/sandbox-policy.yaml` |
| `/opt/agent/skills`, `/opt/agent/contracts` | The skills and the tool registry (read-only) |
| `/opt/data` | `HERMES_HOME`: the rendered `config.yaml`, `SOUL.md`, the Relay config and the `execution-receipts` plugin |
| `/sandbox` | The workspace |

OpenShell ignores an image's `ENTRYPOINT` and `CMD`, so Hermes' own init never runs. The build does its work
instead: it seeds `HERMES_HOME` as the `hermes` user (uid 10000) and runs Hermes' config migration. The main
process, `/opt/hermes/.venv/bin/hermes gateway run`, is passed when the sandbox is created. The build fails if
the policy names any interpreter other than Hermes' own (`/usr/bin/python3.13`), or if the Relay it ships is
0.9 or later.

`AGENT_FEATURES` picks which tools and skills the image carries; `demo.sh` derives it from
`COMPOSE_PROFILES` ([configuration](configuration.md#derived-by-demosh)).

## The policy

Anything the policy does not allow is denied.

| Rule | Allows |
|---|---|
| Filesystem | `/opt/hermes` and `/opt/agent` read-only; `/opt/data` and the workspace read-write; OpenShell's baseline system paths |
| Landlock | `hard_requirement`: the sandbox fails closed on a kernel without it |
| `retrieval_mcp` | `host.openshell.internal:8120/mcp`: the MCP handshake, `tools/list`, `ping`, and `tools/call` for `retrieve_evidence` |
| `market_analytics_mcp` | `:3010/mcp`: the same, for the seven market tools and `predict_asset_outcomes` |
| `auto_ontology_mcp` | `:3003/mcp`: the same, for `ask_question` |
| `phoenix_otlp` | `:6006`: `POST /v1/traces` only |
| Provider `switchyard` | `:4000`: `POST /v1/chat/completions` and `GET /v1/models`. No credential: Switchyard holds the model key |
| Provider `receipts` | `:8000`: `GET …/execution-scope`, `POST …/tool-receipts` and `POST …/llm-calls` under `/internal/hermes/jobs/*` |

Every rule applies to Hermes' interpreter only. The provider rules come from the profiles in
`infra/openshell/providers/`, which OpenShell adds to the effective policy when the sandbox is created with
`--provider`.

**Keys.** The `receipts` provider binds `HERMES_RECEIPT_API_KEY`. Inside the sandbox that variable holds an
opaque placeholder, and the supervisor substitutes the real key only on the three routes above. The Runs API
key (`API_SERVER_KEY`, from `HERMES_API_SERVER_KEY`) is a real value set at creation: Hermes checks it on
inbound requests, and placeholders resolve only on outbound ones. No model key and no `NVIDIA_*` or
`OPENAI_*` variable ever reaches the sandbox.

## Lifecycle

`./scripts/demo.sh up` starts everything the sandbox calls first, because Hermes parks an MCP server it
cannot reach at startup. Then it:

1. starts the gateway (the certificate and preflight one-shots run first) and waits for `/readyz` on 18081;
2. imports the provider profiles and creates the two providers, if missing;
3. creates the sandbox `hermes` and waits until it is `Ready`;
4. starts the `hermes-gateway` forwarder.

The sandbox carries a fingerprint label: a hash of the agent image ID, the tool servers' image IDs, the active
data pack's `pack.yaml` and `DATA_PACK_PROFILE`, the provider profiles, `gateway.toml`, the OpenShell pins,
`AGENT_FEATURES` and the two Hermes keys. `up` and `data prepare` recreate the sandbox only when the fingerprint
changes, so a repeat `up` keeps it. Hermes lists each MCP server's tools once, when it starts, and the pack and
its profile shape those tools (the universes, the relationship graph, whether there are minute bars). So a new
tool image or a `DATA_PACK` or `DATA_PACK_PROFILE` switch must recreate it: otherwise the model keeps seeing the
previous build's tools and fails its calls. `./scripts/demo.sh restart agent` recreates it on demand.
`./scripts/demo.sh down` deletes the sandbox through the gateway before it stops Compose.

The OpenShell CLI and `jq` run in the `openshell-cli` container with their own client volume, so the host
needs only Docker and curl, and the host's `~/.config/openshell` is never read or written.

## Proving the boundary

```bash
./scripts/demo.sh check
```

It runs a script in the sandbox as Hermes' interpreter and passes only if:
the receipt key is a placeholder; egress to a host outside the policy is blocked; Switchyard's model list is
allowed but `POST /v1/responses` is denied; the API's public routes are denied; market analytics'
`POST /benchmark`, which only the API calls, is denied (the policy allows its `/mcp` only); and the plugin's
three internal routes reach the API.

`./scripts/demo.sh logs agent [-f]` shows Hermes' log, including `DENIED` lines with the binary, host and
reason of any refused connection.

## Sharing a Docker host

The demo can run next to other OpenShell gateways, for example NemoClaw, on one Docker host:

- The gateway is named `market-demo`, listens on 18080 and 18081, and labels its sandboxes with the namespace
  `market-demo`. It never lists or touches other namespaces' sandboxes.
- `demo.sh` removes only what it created, never runs `docker compose down --remove-orphans`, and never prunes
  containers, volumes or networks. `down --prune` removes only this project's untagged images, plus Docker's
  unused build cache ([disk](operations.md#disk)).

## Host requirements

Docker Engine 28 or later, and a Docker host kernel with Landlock ABI 3 or later (Linux 6.2+); `doctor` checks
the kernel version. The sandbox also probes seccomp user notification itself and fails closed. amd64 and arm64
both work. On Docker Desktop, turn on host networking and turn off Enhanced Container Isolation.

## Known issues in OpenShell 0.1.2

| Issue | Effect | What the demo does |
|---|---|---|
| `sandbox delete` removes containers without their anonymous volumes | The Hermes image declares `VOLUME /opt/data`, so every sandbox leaves two unlabelled volumes | `demo.sh` records those volumes' names while they are known (in `.demo/`) and removes only them, after checking they hold this profile. It never sweeps dangling volumes |
| The proxy keeps an upstream connection open after the server closed it for being idle | The next request on that connection fails with no response. NeMo Relay does not retry, so it lost trace batches | Phoenix runs with a 120 s keep-alive (`infra/phoenix/serve.py`), longer than Relay keeps an idle connection |
| MCP JSON-RPC request bodies are capped at 64 KiB | Tool arguments larger than that are refused; responses and OTLP exports are not capped | Tool arguments stay small; no change needed |
| `sandbox exec` processes do not inherit the image's `ENV` | Only the sandbox's main process sees it | `check` names the addresses it probes itself |

Remove the Phoenix keep-alive when a release closes a sandbox connection once its upstream goes idle.

## Bumping OpenShell

1. Take the new image digests from anonymous GHCR manifest requests and the CLI checksums from the release's
   `openshell-checksums-sha256.txt`; update `infra/openshell/versions.env`.
2. Update the two runtime image references in `infra/openshell/gateway.toml`.
3. Check that the image references in both files agree, then run `./scripts/demo.sh up`. The pins are part of the fingerprint, so the sandbox is recreated: an old
   supervisor must not outlive a gateway upgrade.
