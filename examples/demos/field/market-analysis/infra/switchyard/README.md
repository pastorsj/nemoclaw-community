<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Switchyard model router

Hermes runs inside the OpenShell sandbox and sends every model call to
[Switchyard](https://github.com/NVIDIA-NeMo/Switchyard) at `host.openshell.internal:4000`, which is
published on `127.0.0.1:4000`. Each call names a route, such as `market-research`, and Switchyard
maps it to real models on the inference endpoint (and, for the capable model, on an optional
endpoint of its own).

- **Only key holder.** Switchyard is the only holder of `INFERENCE_API_KEY` and `CAPABLE_API_KEY`
  on the agent path. The sandbox never sees a key.
- **Loopback only.** Switchyard has no inbound authentication, so never publish it beyond
  loopback.
- **Why a standalone server.** NVIDIA's long-term direction is the NeMo Relay plugin inside
  Hermes. It does not yet pass the session id that the escalation latch needs, so this demo runs
  `switchyard-server` 0.3.0 on its own.
- **Built from source.** There is no official image, so the Dockerfile builds `switchyard-server`
  0.3.0 from crates.io with `--locked`.

| File | Purpose |
|---|---|
| `routes/*.toml.tmpl` | One reviewed Switchyard config per routing mode. `SWITCHYARD_ROUTES` picks one. |
| `judge-prompt.md` | System prompt for the escalation judge. |
| `entrypoint.sh` | Checks the environment, renders the template, runs `--dry-run`, then serves. |

## Routes

Every template serves the same route ids, so Hermes' configuration never changes. Hermes calls
`market-research` and `market-research-aux`, and `market-research-fallback` when the agent's model is
overloaded.

| Route id | Caller | Served by |
|---|---|---|
| `market-research` | every agent turn | depends on the template (below) |
| `market-research-efficient` | bake-off baselines, debugging | the efficient model |
| `market-research-capable` | bake-off baselines, debugging | the capable model (not in `passthrough.nemotron`) |
| `market-research-aux` | Hermes auxiliary calls (compression and similar) | the aux model (`AGENT_AUX_MODEL`, Nemotron 3 Super by default) with thinking off. These calls are never judged. |
| `market-research-fallback` | the rest of a run whose model is overloaded (below) | the aux model, as `market-research-aux` |

| `SWITCHYARD_ROUTES` | `market-research` | For |
|---|---|---|
| `passthrough.nemotron` | efficient, every turn, no judge, no capable model | **default** on build.nvidia.com (Nemotron 3 Ultra) |
| `pinned-capable.nemotron-gpt` | capable GPT, every turn, no judge | a provider that serves GPT-6 Sol; a reference ceiling (the 2026-09-30 bake-off's winner) |
| `escalation.nemotron-gpt` | escalation: efficient → capable GPT (Responses API), judged by a GPT model | efficient model first, GPT-6.1 Sol on escalation (the 2026-10-01 bake-off's winner) |
| `pinned-capable.nemotron-claude` | capable Claude, every turn, no judge | a provider that serves Claude, e.g. Claude Opus 5.5 |
| `escalation.nemotron-claude` | escalation: efficient → capable Claude (Anthropic Messages API), judged by a Claude model | efficient model first, Claude on escalation |
| `escalation.nemotron` | escalation: efficient → capable model (Chat Completions), judged by a Nemotron model | all-Nemotron, e.g. on build.nvidia.com (it has no frontier model) |

The suffix names the model families the template expects. Every template serves Hermes' auxiliary
and fallback calls with the Nemotron aux model.
- `.nemotron-gpt`: the capable model and the escalation judge are GPT models from the capable
  endpoint. GPT tool calling needs the Responses API, so the capable model runs with
  `reasoning_effort = "medium"` and `store = false`; the judge uses the Responses API too, on a client
  of its own, with `reasoning_effort = "low"` and JSON-schema verdicts (`text.format`).
- `.nemotron-claude`: the capable model is Claude, over the Anthropic Messages API (`x-api-key` and
  `anthropic-version` headers, `/v1/messages`). That API has no `reasoning_effort`, so the target sets
  `output_config.effort = "medium"` through `extra_body` instead; Switchyard also marks the prompt for
  Anthropic's prompt caching. The judge is a Claude model on a client of its own; its JSON-schema
  verdict goes in `output_config.format`, which leaves no room for an `output_config.effort` default,
  so the judge runs at the model's default effort.
- `.nemotron`: every model speaks Chat Completions, for example all-Nemotron on build.nvidia.com. Its
  judge is a Nemotron model on the inference endpoint, because build.nvidia.com serves no frontier model.

**The judge and the capable model need different ids.** Switchyard keys a route's targets by model id:
one id on two clients in the same route is rejected, and one id twice on one client must have identical
settings. So a frontier judge cannot share the capable model's id and still have its own client, effort
and deadline. Use another id the provider lists for the same model (for example a second deployment
or a dated snapshot), or another strong model from that provider.

The capable model's client reads `CAPABLE_BASE_URL` and `CAPABLE_API_KEY`. The endpoint defaults to
the inference endpoint, and the key to the inference key on that endpoint only: the inference key
never goes to another host, so with another endpoint and no `CAPABLE_API_KEY` the entrypoint exits 64.
So GPT-6.1 Sol can come from any OpenAI-compatible provider while the efficient and auxiliary models stay
on the inference endpoint (build.nvidia.com by default). In the frontier templates (`*-gpt`, `*-claude`)
the judge is a frontier model on the capable endpoint too, so the turns it rates go to that provider.

**How escalation works.** It uses Switchyard's `llm_classifier` router in `mode = "escalation"`.
On each turn of a session that has not latched:
1. Switchyard calls the efficient model and buffers its reply.
2. The judge rates the completed turn using `judge-prompt.md`. It returns a JSON-schema verdict on
   a client of its own. In the frontier templates the judge is a GPT or Claude model from the capable
   endpoint, with a 120 s deadline for every attempt of one verdict and 2 retries; in the all-Nemotron
   template it is a Nemotron model with thinking off and a 60 s deadline. It escalates only for a failure it can name:
   a stuck or invalid tool loop, an error used as data, or a final report with a missing citation,
   a wrong window or unit, an unanswered part, a contradiction or no evidence.
3. After `SWITCHYARD_CONFIRMATIONS` consecutive "escalate" verdicts, the buffered reply is
   discarded. The capable model serves that turn and every later turn of the session.

Sessions are keyed by the `x-switchyard-session-id` header. Hermes sends its session id, which is
the job id, so every question starts on the efficient model. Latches are held in memory, so a
restart clears them.

What escalation costs:
- one judge call per unlatched turn;
- no token streaming on judged turns;
- if a judge call fails, the agent's turn fails.

`.env.example` sets 1 confirmation. With 2, an escalate verdict on the final report can never
latch, so a weak report is never rescued.

**When a model is overloaded.** build.nvidia.com can answer "Service temporarily overloaded" inside
an HTTP 200 stream. Switchyard 0.3.0 re-routes an in-stream error only when it is a context
overflow, so it passes this one to Hermes. Hermes retries the call twice, then follows its
`fallback_providers` chain (`agent/profile/config.yaml`) to `market-research-fallback`, which serves
the rest of that run with the auxiliary model. The next job starts on `market-research` again. The
switch shows in the execution graph: the router lists the fallback model's calls without a tier.
Without the fallback, Hermes kept retrying for about 5 minutes and then failed the job.

## Environment

| Variable | Required | Meaning |
|---|---|---|
| `SWITCHYARD_ROUTES` | yes | Template name from `routes/`, without `.toml.tmpl`. |
| `INFERENCE_BASE_URL` | yes | OpenAI-compatible base URL, e.g. `https://integrate.api.nvidia.com/v1` (build.nvidia.com). |
| `INFERENCE_API_KEY` | yes | Read from `/run/secrets/inference_api_key` when that file exists (the Compose secret), else from the environment. |
| `CAPABLE_BASE_URL` | no | The capable model's base URL: OpenAI-compatible, or the Anthropic Messages API for `*-claude` (e.g. `https://api.anthropic.com/v1`). Empty means `INFERENCE_BASE_URL`. |
| `CAPABLE_API_KEY` | no | Its key, read from `/run/secrets/capable_api_key` when that file exists, else from the environment. Empty means `INFERENCE_API_KEY` when `CAPABLE_BASE_URL` is empty or equals `INFERENCE_BASE_URL`; with another endpoint a template that uses the capable model needs it. |
| `AGENT_EFFICIENT_MODEL`, `AGENT_AUX_MODEL` | yes | Model ids at the inference endpoint. The aux model (Nemotron 3 Super by default, thinking off) serves Hermes' auxiliary and fallback calls; it must differ from the efficient model. |
| `AGENT_CAPABLE_MODEL` | all but `passthrough.nemotron` | Model id at the capable endpoint. |
| `AGENT_JUDGE_MODEL` | escalation templates | The escalation judge: a model id at the capable endpoint in `*-gpt` and `*-claude`, at the inference endpoint in `escalation.nemotron`. The efficient, capable and judge models must all differ. |
| `SWITCHYARD_CONFIRMATIONS` | escalation templates | `1` or `2`. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | no | For example `http://phoenix:6006/v1/traces`. Set only this one: the generic `OTEL_EXPORTER_OTLP_ENDPOINT` also turns on metrics export, which Phoenix does not accept. |
| `OTEL_SERVICE_NAME` | no | Defaults to `switchyard-server`. |
| `RUST_LOG` | no | `info,switchyard_libsy::algorithms::util::escalation=debug` logs every judge verdict with its reason. |
| `INFERENCE_API_KEY_FILE`, `CAPABLE_API_KEY_FILE`, `SWITCHYARD_STATE_DIR`, `TMPDIR` | no | Paths for running `entrypoint.sh` outside the image. Defaults: the secret paths above, `/var/lib/switchyard` and `/tmp`. |

Configuration mistakes exit 64 with a message before anything listens: an empty variable, equal
model ids (above), a confirmations value other than 1 or 2, or an unknown template. `--dry-run` catches the
rest, such as a malformed URL.

The routing log lives in `/var/lib/switchyard`, the `switchyard-data` volume. It is owned by uid
1000 with mode 0700, so other containers cannot read it; use the HTTP endpoints below instead.

## Switching templates and endpoints

Edit `.env`, then run `./scripts/demo.sh restart switchyard`. The sandbox is not rebuilt, because
Hermes always requests `market-research`. The startup log line names the template, the endpoints
and whether the capable model has its own key.

```dotenv
# Default: build.nvidia.com (nvapi- key), Nemotron 3 Ultra on every turn, Super for auxiliary calls.
INFERENCE_BASE_URL=https://integrate.api.nvidia.com/v1
SWITCHYARD_ROUTES=passthrough.nemotron
AGENT_EFFICIENT_MODEL=nvidia/nemotron-3-ultra-550b-a55b
AGENT_AUX_MODEL=nvidia/nemotron-3-super-120b-a12b

# Ultra escalating to GPT-6.1 Sol, judged by a GPT model, from a provider serving both.
SWITCHYARD_ROUTES=escalation.nemotron-gpt
AGENT_CAPABLE_MODEL=<the GPT-6.1 Sol id at that provider>
AGENT_JUDGE_MODEL=<another id there: the same model's second id, or another strong GPT model>
CAPABLE_BASE_URL=https://<a provider serving the frontier model>/v1
CAPABLE_API_KEY=<that provider's key>

# Ultra escalating to Claude Opus 5.5, judged by a Claude model, over the Anthropic Messages API.
SWITCHYARD_ROUTES=escalation.nemotron-claude
AGENT_CAPABLE_MODEL=claude-opus-5-5
AGENT_JUDGE_MODEL=<another Claude id at that provider>
CAPABLE_BASE_URL=https://<a provider serving the frontier model>/v1
CAPABLE_API_KEY=<that provider's key>

# All-Nemotron escalation on build.nvidia.com: Super answers, Ultra takes over, Lightning judges.
SWITCHYARD_ROUTES=escalation.nemotron
AGENT_EFFICIENT_MODEL=nvidia/nemotron-3-super-120b-a12b
AGENT_CAPABLE_MODEL=nvidia/nemotron-3-ultra-550b-a55b
AGENT_JUDGE_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
AGENT_AUX_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```

## Reading routing.jsonl

`/var/lib/switchyard/routing.jsonl` gets one line per upstream call (abbreviated here):

```json
{"ts":"2026-09-29T08:37:24.648Z","route_id":"market-research","algorithm":"llm_task_classifier","session_id":"<job id>","model":"nvidia/nemotron-3-ultra-550b-a55b","tier":"","prompt_tokens":1300,"completion_tokens":40,"total_tokens":1340}
```

- `tier: ""` rows name the model that served the answer. On `market-research`, a session whose
  served model changes from the efficient id to the capable id has escalated.
- `tier: "classifier"` rows are routing overhead: a judge call, or the efficient reply that was
  discarded on the escalation turn.
- `route_id: "market-research-aux"` rows are Hermes auxiliary calls.

```sh
./scripts/demo.sh logs routing
./scripts/demo.sh logs routing -f | jq -c 'select(.tier == "") | {session_id, route_id, model}'
curl -s "127.0.0.1:4000/v1/routing/session-stats?session_id=<job id>"   # calls and tokens per model, overhead included
curl -s 127.0.0.1:4000/v1/stats                                          # per-model usage and routing overhead
```

The log records no verdicts. Phoenix does: each request's `libsy.run` span carries
`switchyard.route`, `session.id` and `evidence.verdict`:
- `continue`: the judge declined.
- `pending`: the judge escalated, but not enough times yet.
- `escalate`: this turn switched to the capable model.
- `latched`: the session was already on the capable model, so the judge did not run.

For the judge's reasons, set the `RUST_LOG` value above.

## Bake-off

[`docs/models-and-routing.md`](../../docs/models-and-routing.md) has the method, the results and the
recommendation. In short: on build.nvidia.com, which serves no frontier model, the default is
`passthrough.nemotron` (Nemotron 3 Ultra alone). With a provider that serves a frontier model,
`escalation.nemotron-gpt` (Ultra escalating to GPT-6.1 Sol, judged by GPT-6.1 Sol) was the best arm of the
2026-10-01 bake-off on a real-market data pack: 10 and 14 of 16 runs with its two graders, against 7 and 9 for GPT-6.1 Sol
pinned and 9 and 13 for Claude Opus 5.5 pinned, with 10% of the agent turns on Sol. Each arm is an `.env` change
plus `./scripts/demo.sh restart switchyard`, which also resets latches and `/v1/stats`.

## Run

Normally Compose runs this as the `switchyard` service (profile `core`) through
`./scripts/demo.sh up`. To run it on its own, with the settings exported in your shell:

```sh
docker build -t market-demo/switchyard:local infra/switchyard
docker run --rm -p 127.0.0.1:4000:4000 -e SWITCHYARD_ROUTES -e SWITCHYARD_CONFIRMATIONS \
  -e INFERENCE_BASE_URL -e INFERENCE_API_KEY -e CAPABLE_BASE_URL -e CAPABLE_API_KEY \
  -e AGENT_EFFICIENT_MODEL -e AGENT_CAPABLE_MODEL -e AGENT_JUDGE_MODEL -e AGENT_AUX_MODEL \
  market-demo/switchyard:local
curl -s 127.0.0.1:4000/v1/models | jq -r '.data[].id'
```

**Changing a template or the judge prompt.** Both are baked into the image. Rebuild the image; its
entrypoint renders the template and runs `--dry-run` before it serves.

**Upgrading Switchyard.**
1. Check <https://crates.io/crates/switchyard-server> for a new version.
2. Bump `SWITCHYARD_VERSION` in the Dockerfile.
3. Rebuild, and run `./scripts/demo.sh restart switchyard`.
