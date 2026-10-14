<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Operations

Running the demo day to day: the lifecycle commands, Phoenix, jobs and recordings, troubleshooting, and running
it on a Brev VM. Run `./scripts/demo.sh` with no arguments for
the command list.

## Lifecycle

| Command | What it does |
|---|---|
| `./scripts/demo.sh up [--no-build]` | Builds the images, prepares the data, starts the tools, Switchyard, Phoenix and the API, then the sandbox, the Hermes forwarder and the UI. Idempotent: a repeat `up` recreates only what changed and keeps the sandbox |
| `./scripts/demo.sh status` | Services, the sandbox and its endpoints, Switchyard's routes and the URLs |
| `./scripts/demo.sh check` | Proves the sandbox boundary on the running stack ([OpenShell](openshell.md#proving-the-boundary)) |
| `./scripts/demo.sh logs [agent\|routing\|SERVICE...] [-f]` | `agent`: Hermes in the sandbox; `routing`: Switchyard's per-call log; otherwise `docker compose logs` |
| `./scripts/demo.sh restart switchyard` | Applies section 1 of `.env`: re-renders the routes, clears latches and stats, refreshes the API's model ids, and recreates Auto Ontology (its models, endpoint and key) and retrieval (its key defaults to the inference key). The sandbox is kept |
| `./scripts/demo.sh restart agent` | Recreates the sandbox |
| `./scripts/demo.sh restart SERVICE` | `docker compose restart SERVICE` |
| `./scripts/demo.sh down [--volumes] [--prune]` | Deletes the sandbox, then stops everything; `--volumes` also deletes the data, the index, the jobs and the traces; `--prune` also removes this project's untagged images and Docker's unused build cache, which is host-wide ([disk](#disk)) |

The first `up` builds every image (Switchyard compiles from source), downloads about 1.4 GB of SEC EDGAR
filings and embeds about 23,600 chunks through the retriever endpoint; `up` waits up to an hour for it (it took
about 45 minutes with every profile on an A100 VM). Later runs reuse the images, the download cache and the index.

## Phoenix

Phoenix runs at <http://127.0.0.1:6006>, on loopback only. All traces go to the project
`market-analysis-agent`:

| Source | Spans |
|---|---|
| Hermes, through NeMo Relay | The agent turn, each model call and each tool call. Run metadata (`aiq.job.ref`, `hermes.*`) is promoted to span attributes |
| Switchyard | `libsy.run` per request, with `switchyard.route`, `session.id` (the job id) and `evidence.verdict` (`continue`, `pending`, `escalate` or `latched` on escalation templates) |
| Retrieval | `embed`, `search` and `rerank`, under the MCP `tools/call retrieve_evidence` span |

In the UI, each run's execution view links to its trace. The API finds it through
`GET /v1/jobs/async/job/{id}/trace`, which looks the job up by `aiq.job.ref` and falls back to `session.id`;
the link is `<PHOENIX_URL>/redirects/traces/<trace id>`. Set `PHOENIX_URL` in `.env` if the browser reaches
Phoenix at another address, or to empty to hide the link.

**What traces hold.** Relay exports without an allowlist or a collector. Model spans carry the system
instructions and the current turn from the last user message on, including every tool result; only earlier
conversation turns are dropped (`enable_full_payloads = false` in `agent/profile/relay-plugins.toml`). That is
why Phoenix must stay on loopback. Traces persist in the `phoenix-data` volume until `down --volumes`.

The export is bounded (a queue of 512 spans, batches of 32 every second), so a full queue drops new spans
instead of growing. The agent image fails to build with NeMo Relay 0.9 or later, which cannot export plain
HTTP to Phoenix from the sandbox.

**Routing without Phoenix.**

```bash
./scripts/demo.sh logs routing -f                                          # one line per upstream call
curl -s "127.0.0.1:4000/v1/routing/session-stats?session_id=<job id>"      # calls and tokens per model
curl -s 127.0.0.1:4000/v1/stats                                            # totals, errors, routing overhead
```

## Jobs

- One job runs at a time and four more may wait; a sixth gets `429` with `Retry-After`.
- A job has a 1,200 s deadline and Hermes run budgets (idle, no progress, tool calls); on any of them it fails
  with a message the UI shows, and its Hermes run is stopped.
- Cancelling a queued job means it never starts; cancelling a running one stops its Hermes run.
- When the API restarts, it fails every unfinished job ("The API restarted before this job finished; please
  retry.") and stops its Hermes run.
- Finished jobs stay for `JOB_RETENTION_SECONDS` (a day by default).

The API's settings are in [`api/README.md`](../api/README.md#environment).

## Recording and replay

```bash
./scripts/demo.sh record           # ask the featured questions on the running stack; writes the pack's bundle
./scripts/demo.sh replay           # the UI alone on the bundle: no .env, keys, API or GPU
./scripts/demo.sh up               # back to live mode (recreates only the UI)
```

`replay` swaps the UI container into replay mode on the same port. [Data packs](data-packs.md#recordings)
covers the options and what to review before committing a bundle.

## Disk

Measured with `df` on a Brev A100 VM with every profile (`core,retrieval,analytics-gpu,kumo,ontology`, when the
Kumo NIM still ran in the demo stack), starting from an empty Docker host:

| After | Disk used |
|---|---|
| The OS and Brev's tools | 9 GB |
| Building the images: 24 GB of images (15 GB of it the RAPIDS market-analytics image) and 53 GB of build cache, as `docker system df` counts them | 76 GB |
| Pulling the Kumo NIM (about 14 GB to download, 44 GB unpacked), Phoenix, Milvus, pgvector and OpenShell | 115 GB |
| Preparing the data, the corpus and the index | 117 GB |
| One rebuild of the RAPIDS image after a one-line code change | 132 GB |
| Capping the build cache at 30 GB, then an `up` that rebuilt most images | 98 GB, peak 130 GB |
| Another RAPIDS rebuild, with no prune in between | peak 161 GB, 153 GB after |

The Kumo NIM has since moved to a [service](kumo-service.md) of its own, so a demo host no longer pulls its
44 GB. The first `up` then takes about 70 GB: plan for 110 GB free to run the demo, and for 160 GB, or a prune
after each rebuild, if you change code and rebuild images on the host. The CPU tier also builds no RAPIDS image.
`docker system df` shows the current split.

**External data** (a pack of your own that declares external datasets, [data platform](data-platform.md)) sits
outside Docker, in `DATA_SOURCE_DIR` (default `~/market-demo-data`). Plan for the dataset itself, plus, in the
`demo-data` volume, its daily rollup (about 1% of the minute data) and each build. The daily tables grow with symbols times sessions, not with the minute data: 10,000 symbols
over 10 years is about 27 million daily rows, a few GB per build. `doctor` checks that each dataset not yet
fetched fits in `DATA_SOURCE_DIR` with 10% to spare, and `data clean` removes rollups no build uses.

What grows is the build cache. Each rebuild of a changed image adds its new layers, and the RAPIDS image adds
about 15 GB each time, even for a one-line change. With Docker's classic image store, each rebuild also
leaves the previous image untagged. `./scripts/demo.sh down --prune` removes both: this project's untagged
images, and Docker's unused build cache. The build cache is host-wide, not per project, so `--prune` also
clears other projects' unused cache (never their images, containers or volumes), and the next build starts
cold (about 15 minutes with every profile). To trim the cache instead while the stack runs, cap it:

```bash
docker builder prune --force --max-used-space 30gb   # keeps at most 30 GB, the most recently used
```

Either way, the next `up` pays for it. Built images keep their IDs only while their layers are cached, so
after a prune or a cap `up` rebuilds most images and recreates their containers. On the A100 VM, that took
10 minutes after capping the cache at 30 GB; the agent image kept its ID, so the sandbox stayed.

`down --volumes` deletes the data, the index, the jobs and the traces; `up` rebuilds them (about 25 minutes,
most of it embedding the corpus).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `error: no .env yet` | Run `./scripts/demo.sh init`, then add the keys |
| `doctor` reports a problem | Each message names the variable or host requirement; fix it and run `doctor --keys` again |
| `SEC_USER_AGENT is empty` | Set it to a name and an email, or use `DATA_CORPORA=market_regulations` (skips the SEC download) |
| Milvus restarts or is unhealthy | Docker has less than 8 GiB of memory. Give it more, or drop the retrieval profile |
| The Benchmark tab says Milvus runs its CPU index only, on a GPU host | The profiles lack `analytics-gpu` or `retrieval`, or `retrieval-benchmark` could not measure (`up` then warns, and stops a `milvus-gpu` that did not turn healthy): `./scripts/demo.sh logs retrieval-benchmark milvus-gpu`. `./scripts/demo.sh data reindex` measures again. If GPU memory is tight, lower the pool in `tools/retrieval/milvus/gpu.yaml`, or stop `milvus-gpu` once it has measured: the comparison stays, and answers never use it |
| The Benchmark tab's Milvus row says its quality gate did not pass | The comparison measured at `up` missed a recall or agreement gate; `up`'s output lists each profile's recall and overlap. It searches the build's own query vectors, so measuring that build again gives the same recall |
| `sandbox hermes is not Ready after 180s` | The sandbox's recent log is printed just before it. Check the Docker host kernel (Linux 6.2+ with Landlock) and `./scripts/demo.sh logs openshell-preflight openshell` |
| `hermes-gateway` never turns healthy | The sandbox is not Ready, or `HERMES_API_SERVER_KEY` is shorter than 16 characters (run `init`) |
| A tool call fails while the sandbox is Ready | `./scripts/demo.sh check`, then `./scripts/demo.sh logs agent`: `DENIED` lines name the binary, host and reason |
| Model calls fail with 403 | The key's access group does not include a configured model. `doctor --keys` checks only that the endpoint lists it. See `./scripts/demo.sh logs switchyard` |
| `429` when asking a question | The queue is full (one running, four waiting); wait or cancel a job |
| A job fails: "the evidence for its tool calls was not recorded" | The API rejected a receipt that breaks the display-safe limits, or the plugin could not post it. See `./scripts/demo.sh logs api` and `./scripts/demo.sh logs agent` |
| A job fails: "The API restarted before this job finished" | The API restarted during the job; ask again |
| A job fails after about 10 minutes: "Hermes stopped after exceeding its idle budget; please retry." | A model call stalled upstream and never returned, so Hermes waited out `HERMES_RUN_IDLE_TIMEOUT_SECONDS` (600 s). Ask again. It happened once in 17 jobs on the A100 VM |
| The router shows a model without a tier, e.g. `nemotron-3-super-120b-a12b` | The agent's model answered "Service temporarily overloaded" twice, so Hermes finished the run on `market-research-fallback` ([how](../infra/switchyard/README.md#routes)). The next job starts on the usual model |
| A run has no Phoenix link | Phoenix has no span for the job yet (Relay exports every second), or `PHOENIX_URL` is empty |
| Market tools or retrieval answer from old data | `./scripts/demo.sh data prepare` restarts market analytics and retrieval on the new build; after a manual data change, `./scripts/demo.sh restart market-analytics` (or `retrieval`) |
| Every `up` recreates containers or the sandbox | An image got a new ID. Build through `demo.sh`, which turns off provenance attestations; a plain `docker build` retags the image with a different ID |
| `the ontology profile needs the Auto Ontology submodule` | Run `git submodule update --init --checkout vendor/auto-ontology` from this example's directory, or drop the profile |
| `the kumo profile is gone`, or `KUMO_API_KEY is empty` (or `KUMO_RELATIONAL_URL`) | Kumo is configured only by a [Kumo service](kumo-service.md)'s URL and key, both or neither: drop `kumo` from `COMPOSE_PROFILES` and set the two in `.env` section 3 |
| A Kumo answer says the prediction was unavailable | `./scripts/demo.sh logs market-analytics` (or `market-analytics-gpu`) names the reason. An authentication error: `KUMO_API_KEY` is not the service's key; fix it, then `./scripts/demo.sh restart kumo`. Otherwise check the service ([Kumo service](kumo-service.md#expose-it-through-an-https-link)) |

For the sandbox specifically, see the troubleshooting table in
[`infra/openshell/README.md`](../infra/openshell/README.md#troubleshooting).

## Brev VM mode

The GPU tier (`analytics-gpu`) runs on a Linux x86_64 VM with an NVIDIA GPU, such as a Brev A100 instance; the
GPU is the market tools' and the GPU Milvus's alone. Kumo runs elsewhere, as a [Kumo service](kumo-service.md). Nothing about the demo changes: the same `demo.sh`, bound to the VM's loopback, reached
over SSH, or with the UI alone shared through a Brev link (step 8). These steps were run on 2026-09-29
on a fresh Brev A100 (40 GB) VM with 12 vCPUs, 83 GiB of memory and a 533 GB disk (Ubuntu 22.04, kernel 6.8,
Docker 29.8 with Compose 5.5, driver 595, NVIDIA Container Toolkit 1.20, as Brev provisions it), with every
profile and the default corpora. `up` needed nothing else installed.

1. **Check the host.** Docker Engine 28+ with Compose 2.30+, a kernel of 6.2 or later (for Landlock), and the
   NVIDIA Container Toolkit (`docker info` lists the `nvidia` runtime). Plan for 110 GB of free disk with
   every profile ([disk](#disk)).
2. **Get the code** on the VM: clone the NemoClaw Community repository and change to this example's
   directory. For the ontology profile, also run `git submodule update --init --checkout vendor/auto-ontology`
   there; NVIDIA/auto-ontology is public, so the VM needs no GitHub credentials.

   ```bash
   git clone https://github.com/NVIDIA/nemoclaw-community.git
   cd nemoclaw-community/examples/demos/field/market-analysis
   git submodule update --init --checkout vendor/auto-ontology   # ontology profile only
   ```
3. **Configure.** Run `./scripts/demo.sh init` on the VM, then edit `.env` there (it stays mode 600). Keep keys
   off command lines, where the shell history and the process list would see them. Set `INFERENCE_API_KEY`
   (your `nvapi-` key, which also serves the retriever) and `SEC_USER_AGENT`, then the profiles (leave out
   `ontology` without the submodule):

   ```bash
   COMPOSE_PROFILES=core,retrieval,analytics-gpu,ontology
   ```

   For the Kumo questions, also set `KUMO_RELATIONAL_URL` and `KUMO_API_KEY` to a
   [Kumo service](kumo-service.md#point-a-demo-deployment-at-it), which runs on a GPU host of its own.

   **Real data** comes from a pack of your own that declares external datasets ([data packs](data-packs.md)).
   They are fetched once, at setup, before the first `up` (`doctor` stops `up` until they are).
   `DATA_SOURCE_DIR` defaults to `~/market-demo-data`, which on a Brev VM is on its one large disk; point it at a
   separate data disk if the VM has one. Either set `DATA_SOURCE_<ID>` to a URL (a bucket, a signed HTTPS link
   or a Hugging Face dataset, with its credentials in `.env`) and run `./scripts/demo.sh data fetch` on the VM,
   which a Launchable's setup script can do too, or push the data from your machine and verify it on the VM:

   ```bash
   # On your machine
   rsync -a --partial --exclude '.*' <dataset>/ <instance>:market-demo-data/<dataset id>/
   # On the VM
   ./scripts/demo.sh data fetch --verify-only
   ```
4. **Kumo.** The demo host runs no Kumo NIM. Set up the [Kumo service](kumo-service.md) once, on a GPU host of
   its own (sizing, setup, the HTTPS link and key rotation are there), and point the deployment at it (step 3).
   `doctor --keys` (step 5) checks the link and the key together.
5. **Start and prove it.**

   ```bash
   ./scripts/demo.sh doctor --keys
   ./scripts/demo.sh up
   ./scripts/demo.sh check
   ```

   The first `up` with every profile took about 43 minutes on the A100 VM, on 2026-09-29 with the pack the
   repository then used (`market-analysis`, since replaced by `synthetic-market` at the same 2,000-issuer scale):

   | Step | Time |
   |---|---|
   | Building the images (Switchyard compiles from source; the RAPIDS image is 15 GB) | 14 min |
   | Pulling the Kumo NIM (then in the demo stack), Milvus, Phoenix and pgvector | 5 min |
   | The data pack (2,000 issuers) | 1.5 min |
   | Downloading the corpus (SEC EDGAR, eCFR and eight since-dropped briefs: 5,229 documents) | 5.5 min |
   | Embedding and indexing 23,654 chunks through the retriever endpoint | 15 min |
   | The OpenShell gateway, the sandbox, the Hermes forwarder and the UI | 40 s |

   A later `up` that rebuilt only the RAPIDS image took 11 minutes, 9 of them rebuilding it. With nothing
   changed, a repeat `up` took 36 s and recreated only what differed (after `replay`, just the UI), keeping the
   sandbox. `down` took 46 s and the `up` after it 85 s, which recreates the sandbox and reuses the data and
   the index. Updating the code (step 2) and running `up` took 31 s and recreated nothing
   when only documentation changed; a change under `tools/market-analytics` rebuilds the RAPIDS image
   (9 to 11 minutes).

   `synthetic-market` alone, built with `data prepare` and the default corpora, took 17 minutes (the
   structured part 20 s; the corpus 53 s from the download cache; 23,646 chunks 15 minutes).

   Once the stack was up, `check` passed 8 of 8, and the seven hero questions of the retired `market-analysis`
   pack ([models and routing](models-and-routing.md#the-earlier-bake-off-2026-09-29)) each finished in 21 s
   to 3 minutes with citations and a Phoenix trace.
6. **Footprint.** Sampled every 10 s with `df`, `free` and `nvidia-smi` through the
   install:

   | Resource | Whole stack, idle | Peak, and when |
   |---|---|---|
   | Disk | 108 GB above the OS after the first `up` | 152 GB above the OS, after two RAPIDS rebuilds without a prune ([disk](#disk)) |
   | Memory | 9.3 GiB used, 6.7 GiB of it the containers (the Kumo NIM 1.8 GiB, the RAPIDS worker 1.9 GiB) | 14.0 GiB, while `up` built the images |
   | GPU memory | 2.2 GiB before the first prediction, 4.3 GiB after | 5.6 GiB, with the benchmark's extra RAPIDS worker (step 7) running beside the stack |

   These figures include the Kumo NIM, which then ran in the demo stack and has since moved to its own
   [service](kumo-service.md): 1.8 GiB of the memory, and 0.85 GiB of GPU memory at idle, about 3.5 GiB after its
   predictions. Without it, `market-analytics-gpu` and `milvus-gpu` have the GPU to themselves. The RAPIDS worker
   preallocates no memory pool (0.5 GiB at idle); `milvus-gpu` preallocates a 1 GiB pool that may grow to 4 GiB,
   set in `tools/retrieval/milvus/gpu.yaml` ([retrieval](retrieval.md#cpugpu-index-comparison-analytics-gpu)).
   On 2026-10-01, with a `GPU_IVF_FLAT` copy of 32,676 chunks measured, `milvus-gpu` held 4.7 GiB of the
   A100's 40 GiB and the RAPIDS worker 0.5 GiB.
7. **What the GPU buys here.** The RAPIDS worker gives the same answers as pandas. The method: a throwaway container from the stack's own `market-analytics:gpu`
   image, with `--gpus all` and the prepared pack mounted read-only, ran the service's worker with
   `MARKET_ANALYTICS_ENGINE=cpu` and then with `gpu`, never both at once, while the stack sat idle. Each call
   ran once to warm up, then five timed repeats; the time is the tool's own compute timer, the one receipts and
   the explorer show. All nine calls returned the same results on both engines. Median compute per call on the
   A100 (12 vCPUs), before and after the worker moved to tz-naive UTC timestamps
   ([why](../tools/market-analytics/README.md#how-it-fits)):

   | Call | CPU | GPU before | GPU after | CPU/GPU after |
   |---|---|---|---|---|
   | `market_scan`, 2,000 issuers, 2024 to 2026 | 229 ms | 1,560 ms | 77 ms | 3.0x |
   | `market_scan`, 2,000 issuers, volume z-score | 228 ms | 1,556 ms | 82 ms | 2.8x |
   | `market_scan`, 12 reviewed assets, 20 sessions | 86 ms | 686 ms | 56 ms | 1.5x |
   | `market_anomaly_scan`, 12 assets | 76 ms | 502 ms | 41 ms | 1.8x |
   | `market_anomaly_scan`, 2,000 issuers | 291 ms | 1,512 ms | 119 ms | 2.5x |
   | `price_context`, 3 assets | 70 ms | 672 ms | 33 ms | 2.1x |
   | `sentiment_timeline` | 29 ms | 200 ms | 40 ms | 0.7x |
   | `analyze_news_price_relationship` | 746 ms | 2,171 ms | 92 ms | 8.1x |
   | `analyze_market_relationships` | 37 ms | 39 ms | 37 ms | 1.0x |

   The CPU column is the current code; the CPU results are byte-for-byte those of the code before the change.
   Before it, every operation on a frame with a tz-aware timestamp fell back to pandas and paid a failed GPU
   attempt on top. Now no tool call falls back. Only starting the worker does: once
   each for `merge_asof` and `rename_axis` while it loads the pack (cudf.pandas lacks them), and twice while the
   warm-up reads its window. So `CUDF_PANDAS_FAIL_ON_FALLBACK=1` still stops the worker while it loads.
   `sentiment_timeline` stays slower on the GPU and `analyze_market_relationships` breaks even: their work is
   small (22,000 news rows into 11 weekly points; PageRank over 2,000 nodes), so the fixed cost of
   launching GPU kernels and copying results back outweighs the arithmetic. The table and the method are also in the
   [market-analytics README](../tools/market-analytics/README.md#cpu-and-gpu-timings).

   The GPU libraries pay a one-time cost on first use: about 11 s, nearly all of it cudf.pandas compiling
   kernels for the first `market_scan`. (cuml.accel's first PCA added 5 s more, and again for some input
   sizes; that was its input check compiling a kernel, and the anomaly tool now checks its input on the host
   instead.) The worker runs every tool once before it reports ready, so the first question does not pay it:
   after the warm-up, the first call of each shape above, and of 26 other argument shapes, took at most
   270 ms (the 2,000-issuer anomaly scan, usually about 145 ms; the others about 100 ms or less). Starting
   the worker takes about 20 s on the GPU and 8 s on the CPU, warm-up included (the worker's `ready` log line gives
   the warm-up's share). The worker runs one call at a time, so when the agent asks for two tools at once, the second waits
   for the first. The explorer shows each call's device, library and time.
8. **Connect** from your machine with an SSH tunnel; every port stays on the VM's loopback:

   ```bash
   ssh -N -L 3100:127.0.0.1:3100 -L 6006:127.0.0.1:6006 <instance>
   ```

   Then open <http://127.0.0.1:3100> (UI) and <http://127.0.0.1:6006> (Phoenix). With Brev's CLI set up, the
   instance name works as the SSH host.

   **Or share the UI through a Brev link that requires sign-in**, for people without SSH access. The link's
   proxy does not reach the VM's loopback: it connects over Brev's private network interface (`wt0`) to the
   link's port, so with the default binding the link answers 503 (`Connection refused` on the error page).
   Publish the UI, and only the UI, beyond loopback:

   ```bash
   # In .env on the VM
   UI_BIND_HOST=0.0.0.0
   ```

   Then run `./scripts/demo.sh up --no-build`; of the running services, only `ui` changes, so only it is
   recreated. To recreate it alone by hand, `docker compose --env-file infra/openshell/versions.env --env-file .env up -d
   --no-deps ui` (`--no-deps` keeps Compose from recreating the API without the values `demo.sh` derives).

   The link must point at the UI's host port, `UI_PORT` (3100 by default). If the instance has no link yet,
   create one in the Brev console on the instance's page (**Access**, then share or expose port 3100). The UI
   serves the page and proxies `/api/v1` to the API over the Compose network, so the link needs no other
   port. The API, the tools, Switchyard and Phoenix stay on the VM's loopback whatever `UI_BIND_HOST` says,
   and `doctor` and `up` warn while it is not `127.0.0.1`.

   Security: the UI has no sign-in, and through it anyone who can open the link can run the agent and spend
   your inference credits. Whether the link asks for a Brev sign-in is set in the Brev console, not here:
   turn sign-in on before you set `0.0.0.0`, and set it back to `127.0.0.1` when you are done (then
   `up --no-build` again). Never keep `UI_BIND_HOST=0.0.0.0` on a link without sign-in while `.env` holds an
   organization gateway's key: anyone with the URL can run the agent on it. Phoenix stays tunnel-only: the
   UI's **Phoenix** button and its **Open trace in Phoenix** and **Open failed span in Phoenix** links point
   at <http://127.0.0.1:6006> on the viewer's own machine, so they work only for someone running the SSH tunnel
   above.
**Troubleshooting on the VM.** What came up on the A100 VM, besides the [general table](#troubleshooting):

| Symptom | Cause and fix |
|---|---|
| `no space left on device` while building | The build cache grows with each rebuild; cap it or run `down --prune` ([disk](#disk)) |
| The Auto Ontology frontend build logs Prisma `DatabaseNotReachable` | Expected: it pre-renders pages without a database, and the build succeeds |
| `market-analytics-gpu` takes about 25 s to turn healthy | The worker loads the pack and warms up every tool on the GPU first (step 7) |
| `uv: command not found` in `ssh <instance> 'command'` | Brev's uv is in `~/.local/bin`, on the `PATH` of a login shell only; use `ssh <instance> 'bash -lc "command"'` |
| `market-analytics-gpu` stops while the worker loads the pack | `CUDF_PANDAS_FAIL_ON_FALLBACK` is set, and starting the worker falls back four times; unset it (step 7) |

Do not publish the demo's ports on the VM's public interface or through a public port share; the one
exception is the UI behind a link that requires sign-in, such as a Brev link with sign-in set in the Brev
console (step 8). The UI has no sign-in and
spends your inference credits, and Switchyard, Phoenix and the Auto Ontology MCP server have no
authentication at all.
