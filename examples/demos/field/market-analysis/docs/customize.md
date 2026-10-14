<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Customize

The common changes, and every file each one touches. The files that name a tool or an endpoint must agree
with each other, so after any of these run:

```bash
scripts/gen-contracts.sh               # after touching the registry or a Pydantic model (needs uv and Node.js)
./scripts/demo.sh up                   # rebuilds what changed; a new agent image recreates the sandbox
```

## Change the models

Edit section 1 of `.env` and run `./scripts/demo.sh restart switchyard`. The sandbox is kept, because Hermes
always asks for the route `market-research`. The model ids the template uses must all differ, and
`./scripts/demo.sh doctor --keys` checks that each endpoint lists them. The capable model can use its own
endpoint and key (`CAPABLE_BASE_URL`, `CAPABLE_API_KEY`). Which template to use, and how to measure a new model
before adopting it, is in [models and routing](models-and-routing.md).

A new routing template is a new `infra/switchyard/routes/<name>.toml.tmpl` serving `market-research`,
`market-research-aux`, `market-research-fallback` and `market-research-efficient` (plus `market-research-capable`
if it has a capable model),
with a `# requires:` line naming every setting it substitutes. Templates are baked into the image, so rebuild it
with `./scripts/demo.sh up`.

## Change the answer policy

`agent/profile/SOUL.md` is in every system prompt: the workflow, how to pick a skill by capability, the
evidence and citation rules, and the answer format. Keep it free of tool details; those belong in skills. Run
`./scripts/demo.sh up` to rebuild the agent image.

## Add or change a skill

Skills live in `agent/profile/skills/<name>/SKILL.md` in the Agent Skills format: YAML front matter (`name` matching the
directory, a `description` of at most 60 characters, since Hermes truncates longer ones in its skill index,
`license`, `compatibility`, `metadata`), then *When to Use*, *Tool*, *Procedure*, *Pitfalls* and an *Example*. A skill teaches one tool family; `SOUL.md` loads it only when the run's sources grant that family.

1. Write or edit the skill. Say what the application sets (for example `source_ids`) so the model does not
   pass it.
2. A skill that belongs to an optional feature goes in `FEATURES` in `agent/render_config.py`, which hides it
   from images built without that feature.
3. Rebuild the agent with `./scripts/demo.sh up`.

Skills are baked read-only; the agent cannot change them at run time.

## Add a tool to an existing MCP server

For example, an eighth market tool on the `market_analytics` server:

1. Implement it in the server (`tools/market-analytics/src/market_analytics/`) with a typed result and the
   MCP annotation `read_only_hint=True` (Hermes replays read-only calls after a server restart).
2. Add an entry to `contracts/tool-registry.json`: `id` (the MCP tool name), `server`, `hermes_name`
   (`mcp__<server>__<id>`), `family`, `label` and `description` for the UI, `explorer`, `receipt_kind`,
   `feature` (the agent feature that ships it) and `pills` (the technology pills of a run that uses it; a market
   tool has `cudf`). Run `scripts/gen-contracts.sh`, which regenerates the UI's `TOOL_REGISTRY`.
3. Add the tool to the server's `tools.include` in `agent/profile/config.yaml`.
4. Add it to the server's `tools/call` allowlist in `agent/sandbox-policy.yaml`.
5. Teach it in the family's skill.

With an existing `receipt_kind`, the API, the plugin and the UI need no change: the plugin posts the result as
that kind of receipt and the UI opens it in the matching explorer. The plugin blocks any MCP tool that is not
in the registry.

## Add an MCP server

Everything above, plus:

| Where | What |
|---|---|
| `tools/<name>/` | A self-contained service: its own `uv` project and `uv.lock` (Python 3.12), a `Dockerfile` without `EXPOSE` and a `README.md`. Serve MCP over streamable HTTP at `/mcp` and a `GET /health` |
| `compose.yaml` | A service under a new profile, published on `127.0.0.1:<port>` only; mount `demo-data:/data:ro` if it reads the pack |
| `contracts/tool-registry.schema.json` | The new `server` and `feature` values |
| `agent/render_config.py` | A feature: its MCP server and its skill |
| `agent/profile/config.yaml` | The server under `mcp_servers` (`http://host.openshell.internal:<port>/mcp`) and in `platform_toolsets.api_server` |
| `agent/sandbox-policy.yaml` | A network policy for `host.openshell.internal:<port>`, `protocol: mcp`, the handshake rules and the tool allowlist, binaries `*hermes` |
| `scripts/lib/env.sh` | `agent_features`: map the profile to the feature |
| `scripts/lib/doctor.sh` | The profile in `KNOWN_PROFILES`, its port in `check_ports` |
| `scripts/lib/openshell.sh` | The server's image in `TOOL_IMAGES`, so a new image of it recreates the sandbox (Hermes lists a server's tools once, when the sandbox starts) |
| `scripts/demo.sh` | The service in `backend_services` |
| `.env.example` | The profile in section 3 |

## Add a tool family or a receipt kind

A new **family** (a new kind of capability) also needs: the family in `contracts/tool-registry.schema.json`;
its component in `COMPONENT_BY_FAMILY` (`api/src/demo_api/events/execution.py`); a row in the capability table
of `agent/profile/SOUL.md`; its resource node in `ui/src/features/execution/registry.ts`; and a source in a
pack's `pack.yaml` that grants it.

A new **receipt kind** also needs: a receipt model in `api/src/demo_api/receipts/` (then
`scripts/gen-contracts.sh`); the plugin's projection of the tool
result into receipt content (`agent/profile/plugins/execution_receipts/`); and in the UI, a builder under
`ui/src/features/execution/explorers/`, a `case` in `explorers/index.ts` and an entry in `ARTIFACT_KINDS`
(`contract.ts`), which TypeScript then requires. Receipt content must fit the display-safe limits in
[`contracts/README.md`](../contracts/README.md#display-safe-json-limits).

## Change the data

Add or edit a data pack; see [data packs](data-packs.md). No code changes are needed while the pack satisfies
its tools' contracts.

## Change the UI

The UI is the upstream AI-Q UI with the changes listed in [`ui/UPSTREAM.md`](../ui/UPSTREAM.md). Keep the
product name and title. To work on it against the running stack:

```bash
cd ui
npm ci
cp .env.example .env.local    # API_URL=http://127.0.0.1:8000, PACKS_DIR=../data/packs
npm run dev                   # http://127.0.0.1:3000
```

Set `UI_MODE=replay` in `.env.local` to work on replay mode without the API. `./scripts/demo.sh up` rebuilds
the container.
