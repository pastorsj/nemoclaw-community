# Agent

The research agent is [Hermes Agent](https://github.com/NousResearch/hermes-agent)
0.21.5 (image `nousresearch/hermes-agent:v2026.9.24`), packaged as the image
that OpenShell runs in its sandbox (`market-demo/hermes-sandbox:local`). The job
API drives it through the Hermes Runs API; see `infra/openshell/` for how the
sandbox is created and reached.

## Where things live

| Concern | File | Notes |
|---|---|---|
| Model route, tools, API server | `profile/config.yaml` | One config with every optional tool; `render_config.py` narrows it per image. |
| Always-on answer policy | `profile/SOUL.md` | In every system prompt: workflow, evidence, citations, answer format. |
| How to use each tool | `profile/skills/*/SKILL.md` | Loaded on demand; baked read-only at `/opt/agent/skills`. |
| Receipts and source scope | `profile/plugins/` | The `execution-receipts` Hermes plugin, baked into `/opt/data/plugins`. |
| Tracing | `profile/relay-plugins.toml` | NeMo Relay sends OpenInference traces to Phoenix. `enable_full_payloads` is one line to flip. |
| Network and filesystem limits | `sandbox-policy.yaml` | Baked at `/etc/openshell/policy.yaml`. Switchyard and the receipt API are allowed by the provider profiles in `infra/openshell/providers/`. |
| Changes to Hermes itself | `patches/` | Three Runs API patches and one MCP discovery fix; see `patches/README.md`. |
| Profile manifest | `profile/distribution.yaml` | Hermes profile distribution metadata. |

Hermes reaches everything as `host.openshell.internal:<port>`: Switchyard
`:4000` (`/v1`), the job API `:8000`, retrieval MCP `:8120`, market analytics
MCP `:3010`, Auto Ontology MCP `:3003` and Phoenix `:6006`.

## Features

`AGENT_FEATURES` (build argument, default `retrieval,analytics`) picks the
optional tools baked into an image: `retrieval`, `analytics`, `kumo` (needs
`analytics`) and `ontology`. A feature that is off has no toolset, MCP server,
tool or visible skill. Change it by rebuilding the image and recreating the
sandbox.

`demo.sh` derives it from `COMPOSE_PROFILES` and the Kumo settings: it keeps
`retrieval`, `analytics` and `ontology`, maps `analytics-gpu` to `analytics`,
ignores every other profile, and adds `kumo` when `KUMO_RELATIONAL_URL` and
`KUMO_API_KEY` are both set ([Kumo service](../docs/kumo-service.md)). For
example, `core,retrieval,analytics-gpu` with both set becomes
`retrieval,analytics,kumo`. A profile passed through unmapped, such as `core`,
fails the build as an unknown feature.

## Run contract

The job API starts each job with `POST /v1/runs`. `SOUL.md` holds every static
rule, so a run carries only what changes per job:

- `instructions`: the selected source IDs, plus the selected-source catalog as
  JSON. Each source's `capabilities` are `family` values from
  `contracts/tool-registry.json` (`unstructured_retrieval`, `market_analytics`,
  `structured_retrieval`, `structured_prediction`); `SOUL.md` picks skills by
  those names. Do not name an `answering-with-evidence` skill: `SOUL.md`
  replaced it, and the image has no such skill.
- `enabled_toolsets` (patch 0002): always `skills`, plus the registry `server`
  of every tool whose family is selected. For example,
  `unstructured_retrieval,structured_prediction` gives
  `["skills", "retrieval", "market_analytics"]`. There is no `skills_readonly`
  toolset. Hermes rejects any toolset the image's config does not list.

## Build

The build context is `agent/`; the tool registry comes from the named context
`contracts`:

```bash
docker build --build-context contracts=contracts \
  --build-arg AGENT_FEATURES=retrieval,analytics,kumo \
  -t market-demo/hermes-sandbox:local agent
```

The build applies the patches and renders the config. OpenShell ignores the
image's entrypoint, so Hermes' s6 init never runs; the build does its work
instead: it seeds `HERMES_HOME=/opt/data` as the `hermes` user (uid 10000) with
only the essential bundled skill, and runs Hermes' config migration. The build
fails if the policy names any interpreter other than Hermes' own
(`/usr/bin/python3.13`), if Hermes would block `SOUL.md`, or if `skill_manage`
writes would apply instead of being staged.

Profile files are root-owned and read-only to the `hermes` user. `/sandbox` is
the workspace, because `/opt/data` is an image volume.

## Runtime environment

| Variable | Source |
|---|---|
| `API_SERVER_KEY` | `openshell sandbox create --env "API_SERVER_KEY=$HERMES_API_SERVER_KEY"`, from `.env`. At least 16 characters, or Hermes' API server refuses to start. The job API sends it as a bearer token. |
| `HERMES_RECEIPT_API_KEY` | The `receipts` provider: an OpenShell placeholder that the supervisor swaps for the real key on the three `/internal/hermes` routes only. The plugin sends it as `X-Receipt-Key`. |
| `HERMES_RECEIPT_API_URL` | Image: `http://host.openshell.internal:8000` |
| `SWITCHYARD_CLIENT_API_KEY` | Image: `not-a-secret`. Switchyard holds the model key. |
| `HERMES_STREAM_READ_TIMEOUT`, `HERMES_STREAM_STALE_TIMEOUT` | Image: 900 and 600 s, because the escalation router buffers replies. |
| `HERMES_NEMO_RELAY_PLUGINS_TOML` | Image: `/opt/data/relay-plugins.toml` |

No model key or `NVIDIA_*`/`OPENAI_*` variable ever reaches the sandbox.

## Wiring

Keep the wiring consistent by hand: every URL the agent calls must be allowed by
the policy or a provider profile, the MCP tool allowlists must match the config
and `contracts/tool-registry.json`, Switchyard must serve every configured
model, and `compose.yaml` publishes every port on `127.0.0.1`. Skills follow the
Agent Skills spec, and Hermes limits a skill's index entry to 60 characters.

## Adding a tool

Add it to the MCP server, then to `contracts/tool-registry.json`, the server's
`tools.include` in `profile/config.yaml`, the server's `tools/call` allowlist in
`sandbox-policy.yaml`, and the skill that teaches it. A new server also needs a
feature in `render_config.py`, a loopback port in `compose.yaml`, and its image
in `TOOL_IMAGES` (`scripts/lib/openshell.sh`);
[customize.md](../docs/customize.md#add-an-mcp-server) lists every file.
