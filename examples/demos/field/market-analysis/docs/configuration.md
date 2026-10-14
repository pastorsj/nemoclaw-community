<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Configuration

All configuration lives in `.env` in this example's directory. `./scripts/demo.sh init` creates it from
[`.env.example`](../.env.example) with mode 600 and fills in the internal secrets; you add one build.nvidia.com
(`nvapi-`) key. `.env` is gitignored and must never be committed. `./scripts/demo.sh replay` needs no `.env` at all.

## How `.env` is read

- `demo.sh` never sources `.env`, because bash and Compose parse it differently. It reads the values from
  `docker compose config --environment`, so both see the same thing.
- A variable set in your shell wins over `.env`, as in Compose:
  `COMPOSE_PROFILES=core ./scripts/demo.sh up`.
- Keep comments on their own lines, never after a value.
- To run Compose by hand, pass the OpenShell pins along with `.env` (passing `--env-file` turns off Compose's
  own `.env` lookup):

  ```bash
  docker compose --env-file infra/openshell/versions.env --env-file .env ps
  ```

  A raw `up` skips what `demo.sh` derives (below) and the sandbox bring-up, so use `demo.sh` for anything that
  starts services.

## Variables

### 1. Inference endpoint

The agent's models (through Switchyard) and Auto Ontology's reasoning models.

| Variable | Default in `.env.example` | Meaning |
|---|---|---|
| `INFERENCE_BASE_URL` | `https://integrate.api.nvidia.com/v1` | OpenAI-compatible endpoint: build.nvidia.com, or any other OpenAI-compatible endpoint |
| `INFERENCE_API_KEY` | – | Key for that endpoint (`nvapi-` for build.nvidia.com). Read by Switchyard, Auto Ontology and, when `RETRIEVER_API_KEY` is empty, the retrieval tools |
| `SWITCHYARD_ROUTES` | `passthrough.nemotron` | The routing template in `infra/switchyard/routes/` |
| `AGENT_EFFICIENT_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | The passthrough model; the model an escalation template tries first |
| `AGENT_AUX_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Hermes' auxiliary calls and the overload fallback (thinking off), in every template. Must differ from the efficient model |
| `AGENT_CAPABLE_MODEL` | – (commented) | The model a session escalates to; the pinned model. Unused by `passthrough.nemotron` |
| `AGENT_JUDGE_MODEL` | – (commented) | The escalation judge, escalation templates only: a frontier model's id at `CAPABLE_BASE_URL` in `*-gpt` and `*-claude` (another id than `AGENT_CAPABLE_MODEL`), a Nemotron id in `escalation.nemotron` |
| `CAPABLE_BASE_URL`, `CAPABLE_API_KEY` | – (commented) | The capable model's own endpoint and key: OpenAI-compatible, or the Anthropic Messages API for the `*-claude` templates. An empty endpoint means `INFERENCE_BASE_URL`. An empty key means `INFERENCE_API_KEY` only when the endpoint is empty or the same as `INFERENCE_BASE_URL`; another endpoint needs its own key, so the inference key never goes to another host |
| `SWITCHYARD_CONFIRMATIONS` | `1` | Consecutive "escalate" verdicts before a session switches (1 or 2) |
| `AUTO_ONTOLOGY_REASONING_MODEL`, `AUTO_ONTOLOGY_NON_REASONING_MODEL` | `nvidia/nemotron-3-super-120b-a12b`, `nvidia/nemotron-3.5-lightning-30b-a3b` | Auto Ontology's models (ontology profile) |

The efficient, capable and judge models a template uses must all differ, and the aux model must differ
from the efficient one. Which combination to use, and why the default is Nemotron 3
Ultra alone, is in [models and routing](models-and-routing.md).

**A capable model from another provider.** build.nvidia.com serves no GPT model, so the `*-gpt` templates
(`escalation.nemotron-gpt`, `pinned-capable.nemotron-gpt`) need `CAPABLE_BASE_URL` and `CAPABLE_API_KEY`
pointed at an OpenAI-compatible provider that serves `AGENT_CAPABLE_MODEL` over the Responses API. The
capable model and, in the escalation template, the judge go there; the efficient and aux models stay on
`INFERENCE_BASE_URL`. The `*-claude`
templates (`escalation.nemotron-claude`, `pinned-capable.nemotron-claude`) work the same way with a Claude
model, such as Claude Opus 5.5, from an endpoint that serves it over the Anthropic Messages API: Anthropic's
API (`CAPABLE_BASE_URL=https://api.anthropic.com/v1`) or a gateway that speaks that API. Then:

```bash
./scripts/demo.sh doctor --keys
./scripts/demo.sh restart switchyard   # re-renders the routes; the sandbox is kept
```

**Another OpenAI-compatible endpoint for every model**, for example a gateway serving both Nemotron and GPT-6
models: set `INFERENCE_BASE_URL` and `INFERENCE_API_KEY` to it, leave `CAPABLE_BASE_URL` and `CAPABLE_API_KEY`
empty, and use the model ids that endpoint lists. Set `RETRIEVER_API_KEY` (section 2) to an `nvapi-` key if
that endpoint's key is not one.

`doctor` checks only the models the selected template uses. On build.nvidia.com it rejects a non-`nvapi-`
key, a model id that is not `publisher/model`, and a `*-gpt` template whose capable model would also go to
build.nvidia.com. `doctor --keys` asks each endpoint for its model list and looks for every id. Under the
ontology profile, run `./scripts/demo.sh up` after a change as well: Auto Ontology reads the inference
endpoint, and `up` recreates its services with the new values.

### 2. Retriever endpoint

Document embedding and reranking, for retrieval and Auto Ontology. It is separate from the inference endpoint,
so section 1 can point at an endpoint that has no route for the VL reranker.

| Variable | Default | Meaning |
|---|---|---|
| `RETRIEVER_BASE_URL` | `https://integrate.api.nvidia.com/v1` | build.nvidia.com or a self-hosted NIM |
| `RETRIEVER_API_KEY` | empty | Key for that endpoint. Empty means `INFERENCE_API_KEY`, so one `nvapi-` key serves both by default |
| `RETRIEVER_EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | Embedding model |
| `RETRIEVER_RERANK_MODEL` | `nvidia/llama-nemotron-rerank-vl-1b-v2` | Reranking model |
| `RETRIEVER_RERANK_URL` | empty | Full rerank URL, e.g. a self-hosted NIM's `http://<host>:8000/v1/ranking`. Empty means the model's hosted build.nvidia.com endpoint |

Changing the embed model or the base URL changes the index: run `./scripts/demo.sh data reindex`.
[Retrieval](retrieval.md) explains how the endpoints are used.

### 3. What runs

| Variable | Default | Meaning |
|---|---|---|
| `COMPOSE_PROFILES` | `core,retrieval,analytics` | The profiles to run (below). Commands that run the stack always add `core` |
| `KUMO_RELATIONAL_URL` | empty | The `https://` URL of a [Kumo service](kumo-service.md): the Kumo Relational NIM behind a key-checking proxy, on a GPU host of its own. With `KUMO_API_KEY`, gives the agent `predict_asset_outcomes` (needs `analytics` or `analytics-gpu`) |
| `KUMO_API_KEY` | empty | That service's key, sent as `X-API-Key`; it reaches market analytics only, as a Compose secret. Set both or neither: one without the other stops `doctor` and `up`. With neither, Kumo is off and its questions are not offered. After changing either on a running stack where Kumo stays on, run `./scripts/demo.sh restart kumo`; to turn Kumo on or off, `up` |
| `UI_PORT` | `3100` | The UI's host port, on `UI_BIND_HOST` |
| `UI_BIND_HOST` | `127.0.0.1` | The host address the UI is published on. `0.0.0.0` exposes the UI, and through its `/api/v1` proxy the agent, with no sign-in of its own: use it only behind a link that requires sign-in, such as a Brev link with sign-in set in the Brev console ([Brev VM mode](operations.md#brev-vm-mode)). `doctor` and `up` warn while it is set. Every other port stays on 127.0.0.1 |

### 4. Data pack

| Variable | Default | Meaning |
|---|---|---|
| `DATA_PACK` | `synthetic-market` | A directory under `data/packs/`: `synthetic-market` (fictional, made with NeMo Data Designer; the default, since it needs nothing fetched), or a pack of your own ([data packs](data-packs.md#adding-or-swapping-a-pack)) |
| `DATA_PACK_PROFILE` | the pack's default (`standard`) | `synthetic-market`'s scale: `standard` (2,000 issuers, daily bars), `interactive` (50, fast), `ci` (12, minute bars), `intraday` (500, minute bars) or `large` (10,000) |
| `DATA_CORPORA` | the pack's defaults | Comma-separated corpus sources; empty means `sec_filings` (SEC EDGAR) and `market_regulations` (eCFR and the SEC's 2023 cybersecurity rule). A pack may offer opt-in sources, such as `world_news` (GDELT headlines), used only when named |
| `SEC_USER_AGENT` | – | A name and an email, required by SEC's fair-access policy for the EDGAR filings corpus and for SEC company data |
| `DATA_SOURCE_DIR` | `$HOME/market-demo-data` | Where external datasets live on the host, outside the repository: one directory per dataset, mounted read-only at `/sources`. On a VM, the large disk |
| `DATA_SOURCE_<ID>` | – | Where `data fetch` gets external dataset `<id>` (upper case, `-` as `_`; a dataset `minute-bars` reads `DATA_SOURCE_MINUTE_BARS`): a directory or `host:/path` (rsync), or an `https`, `s3`, `gs` or `hf` URL. Empty: verify what is in place |
| `DATA_SOURCE_HTTP_TOKEN`, `AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `HF_TOKEN` | – | Credentials for those URLs, only when the source needs them. Each reaches only the one-shot fetch run, and only for its scheme |
| `DATA_DUCKDB_MEMORY` | DuckDB's default | A memory cap for the data build, e.g. `8GB`; past it the rollup spills to the data volume |

After changing `DATA_PACK` or `DATA_PACK_PROFILE` on a running stack, run `./scripts/demo.sh up` (and
`data fetch` first for a new external dataset). It rebuilds the data, and recreates what names the pack's
database, such as Auto Ontology, which keeps one database per pack, and the sandbox, so Hermes lists the new
build's tool schemas. After changing only `DATA_CORPORA`, `./scripts/demo.sh data prepare` is enough. See
[data packs](data-packs.md) and [data platform](data-platform.md).

### 5. Internal secrets

Generated by `init` (64 random hex characters each); `init` on an existing `.env` fills only empty ones.

| Variable | Meaning |
|---|---|
| `HERMES_API_SERVER_KEY` | The API's bearer key for Hermes' Runs API in the sandbox. At least 16 characters, or Hermes' API server does not start |
| `HERMES_RECEIPT_API_KEY` | The key on the plugin's calls to the API's `/internal/hermes` routes. Hermes only sees an OpenShell placeholder. Must differ from the server key |
| `AUTO_ONTOLOGY_ADMIN_PASSWORD`, `AUTO_ONTOLOGY_AUTH_SECRET` | Auto Ontology's local admin password (the API signs in with it) and session secret |

Changing either Hermes key recreates the sandbox on the next `up`.

### 6. Optional

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RETENTION_SECONDS` | `86400` | How long finished jobs stay available |
| `MARKET_ANALYTICS_TIMEOUT_SECONDS` | `120` | How long one market tool call may run before its worker is replaced |
| `MARKET_ANALYTICS_BATCH_BYTES` | empty | The most an `intraday_scan` reads at once, in bytes; empty means 256 MiB on the CPU and 1 GiB on the GPU. A CPU scan peaks at 8 to 14 times it, so raise it only with memory to spare |
| `PHOENIX_URL` | `http://127.0.0.1:6006` | The Phoenix address the browser links to; empty hides the link |
| `SPEECH_INPUT_ENABLED` | `false` | Voice input: a microphone in the composer; the API transcribes with NVIDIA Nemotron ASR on build.nvidia.com ([`api/README.md`](../api/README.md#voice-input)) |
| `SPEECH_API_KEY` | empty | An nvapi- key for the ASR; empty uses `RETRIEVER_API_KEY` when the retriever is build.nvidia.com |
| `SPEECH_CLEANUP_MODEL` | empty | A public model on build.nvidia.com that deletes fillers and false starts from a transcript, e.g. `nvidia/nemotron-3-super-120b-a12b`; empty means no cleanup |
| `SPEECH_INPUT_MAX_SECONDS` | `60` | The longest recording, 1 to 90 seconds |
| `DATA_DESIGNER_API_KEY` | `INFERENCE_API_KEY`, only when `DATA_DESIGNER_BASE_URL` is `INFERENCE_BASE_URL` or for an nvapi- key on build.nvidia.com | The key for `demo.sh data generate`, which writes the `synthetic-market` pack's text with NeMo Data Designer ([`data/generate/README.md`](../data/generate/README.md)). Set it for any other endpoint: the inference key never goes to another host. Building a pack never needs it |
| `DATA_DESIGNER_BASE_URL` | `https://integrate.api.nvidia.com/v1` | The OpenAI-compatible endpoint `data generate` calls |
| `DATA_DESIGNER_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | The model `data generate` calls, with thinking off |
| `DATA_DESIGNER_PARALLEL` | `8` | Concurrent requests during `data generate` |

### Derived by `demo.sh`

Compose cannot compute these, so `demo.sh` exports them before every Compose call:

| Variable | Value |
|---|---|
| `COMPOSE_PROFILES` | `core,retrieval,analytics` when unset |
| `AGENT_FEATURES` | The tools baked into the agent image: `retrieval`, `analytics` (also for analytics-gpu) and `ontology` from the profiles, and `kumo` when `KUMO_RELATIONAL_URL` and `KUMO_API_KEY` are both set |
| `AUTO_ONTOLOGY_URL` | `http://auto-ontology-frontend:3000` under the ontology profile, for the API's ontology view |
| `DATA_DATABASE_NAME` | The pack id in snake case (`synthetic-market` → `synthetic_market`) |
| `SPEECH_API_KEY` | `RETRIEVER_API_KEY` when empty and `RETRIEVER_BASE_URL` is build.nvidia.com (the ASR's host) |

The services' own settings (queue sizes, Hermes run budgets, timeouts) have working defaults and are
documented in each component's README, for example [`api/README.md`](../api/README.md#environment).

## Profiles

| Profile | Adds | Needs |
|---|---|---|
| `core` (always) | UI, API, OpenShell and the Hermes sandbox, Switchyard, Phoenix, the data build | the inference key |
| `retrieval` | Milvus, the document corpus and index, `retrieve_evidence` | the retriever key; `SEC_USER_AGENT` for `sec_filings` |
| `analytics` | the seven market tools on CPU (pandas, scikit-learn, NetworkX) | – |
| `analytics-gpu` | the same tools on RAPIDS (cuDF, cuML, nx-cugraph), with the same answers; never together with `analytics`. On an A100 at 2,000 issuers, 1.5x to 8.1x faster than the CPU tools on seven of nine measured calls, the two smallest breaking even or running slower ([measured](operations.md#brev-vm-mode)). With `retrieval`, also a GPU Milvus holding a `GPU_IVF_FLAT` copy of the index, for the Benchmark tab's CPU/GPU Milvus comparison only: answers still come from the CPU index ([retrieval](retrieval.md#cpugpu-index-comparison-analytics-gpu)) | Linux, an NVIDIA GPU with driver 535 or newer, the NVIDIA Container Toolkit; 3.6 GB more image for the GPU Milvus |
| `ontology` | Auto Ontology and `ask_question`: required, it answers the structured questions | the `vendor/auto-ontology` submodule (`git submodule update --init --checkout vendor/auto-ontology`) |
| `replay` | the UI alone, on the recorded sessions | nothing |

After changing `COMPOSE_PROFILES`, run `./scripts/demo.sh up`: it rebuilds the agent image with the matching
tools and skills, and recreates the sandbox because the image changed.

## Hardware tiers

| Tier | Profiles | Host |
|---|---|---|
| Replay | `replay` (`./scripts/demo.sh replay`) | Any Docker host; no keys, no GPU |
| CPU | `core,retrieval,analytics` (the default) | Linux kernel 6.2 or later on the Docker host (OpenShell needs Landlock ABI 3), Docker Engine 28+, Compose 2.30+, at least 8 GiB of memory for Docker (Milvus), x86_64 or arm64. Verified on macOS with colima (arm64, 4 CPU, 9 GiB) |
| GPU | `core,retrieval,analytics-gpu` | Linux x86_64 with an NVIDIA GPU, driver 535 or newer and the NVIDIA Container Toolkit. The GPU is the market tools' and the GPU Milvus's alone. Plan for 110 GB of free disk, or 160 GB to rebuild images on the host ([disk](operations.md#disk)). On a 40 GB A100 the RAPIDS worker held about 0.5 GiB of GPU memory and the GPU Milvus 4.7 GiB with a 32,676-chunk index (its pool starts at 1 GiB and may grow to 4 GiB, [footprint](operations.md#brev-vm-mode)). Target: a Brev A100 VM ([operations](operations.md#brev-vm-mode)) |

In every tier, Kumo prediction is a separate [Kumo service](kumo-service.md) on a GPU host of its own, which any
number of deployments share: set its URL and key (section 3). The language, embedding and rerank models are
always hosted, so no model runs in the demo stack. On macOS, Docker Desktop needs host networking on and Enhanced Container Isolation off
(an OpenShell requirement, not verified here); colima needs neither.

## `doctor`

`./scripts/demo.sh doctor` checks the host (Docker, Compose and kernel versions, the NVIDIA runtime for the
GPU profile, Docker memory for Milvus), that the published ports are free (skipped
while the stack runs), and that
`.env` is consistent: profile rules, required keys, distinct model ids for the template, an existing template,
key and model id shapes on build.nvidia.com, `SEC_USER_AGENT` for `sec_filings`, the Kumo URL and key (both or
neither, an `https://` URL, a market-analytics profile; with neither, one line says how to turn Kumo on), and the
generated secrets. `up` runs the same checks except ports and keys. Messages name variables, never their values.

`doctor --keys` also asks each endpoint for its model list with your key and looks for every id the selected
template uses. It checks listing, not access: some gateways list models outside a key's access group, and
those still return 403 when called. The hosted rerankers are not listed at all; the first index build checks the reranker.
With Kumo configured, it also asks the Kumo service for its model list with `KUMO_API_KEY`, which checks the link
and the key together.

## State and exit codes

- `.demo/` holds `demo.sh`'s lifecycle state (the OpenShell volume records described in
  [OpenShell](openshell.md#known-issues-in-openshell-012)). It ignores itself.
- `.build/` holds the patched Auto Ontology source that `tools/auto-ontology/prepare.sh` writes.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | A check failed (`check`, a question `record` could not answer) |
| 2 | Usage error |
| 64 | `.env` or profile problem (`doctor`) |
| 69 | A host requirement or a service is missing (Docker unreachable, gateway or sandbox not ready) |
