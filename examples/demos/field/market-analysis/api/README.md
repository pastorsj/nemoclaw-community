<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Job API

A FastAPI service (`demo_api`) between the UI and the agent. It turns each question into a job, runs
the job on Hermes through the Hermes Runs API, stores what happens as `execution.v2` events, and
serves the answer, its evidence and a read-only view of the pack's database. It also replays a
finished job's market calls on the CPU and the GPU (the Benchmark tab, with the Milvus index comparison on a GPU
host), and transcribes voice input.

```text
UI ──/api/v1 proxy──▶ API ──Runs API──▶ hermes-gateway ──▶ Hermes (OpenShell sandbox)
                       ▲                                        │
                       └──── /internal/hermes (receipts plugin) ◀┘
```

## How a job runs

1. `POST /v1/jobs/async/submit` checks the selected sources and queues the job. One job runs at a time
   and four more may wait; a sixth live job gets `429` with `Retry-After`.
2. The runner starts a Hermes run (`src/demo_api/jobs/executor.py`). The request follows the
   agent's "Run contract" (`agent/README.md`): model `enterprise-research`, `session_id` = job id,
   `aiq.*` metadata that joins the Phoenix trace, and `enabled_toolsets` = `skills` plus the MCP
   servers of the selected sources' families. Earlier answers of the same conversation go along as
   history.
3. Each Hermes event becomes one `execution.v2` event (`hermes/normalizer.py`). The agent's
   `execution-receipts` plugin posts one receipt per data-tool call to
   `/internal/hermes/jobs/{id}/tool-receipts`; the API stores it once and adds one event that points at it.
4. When the run completes, the API waits up to `HERMES_RECEIPT_SETTLE_SECONDS` for every tool call's
   receipt, turns the agent's `[evidence:<receipt id>]` tokens into numbered citations with a Sources
   list (`reports/publication.py`), records the publication as three last events (response formatted, citations
   resolved, run metrics; [contracts](../contracts/README.md#execution-events-executionv2)), and stores the report
   with `success` in one transaction.

A job fails with a message the UI can show when Hermes fails, a progress budget runs out (idle,
no progress, tool calls, repeated events), or the 1,200 s job deadline passes; the Hermes run is
stopped in every case. Cancelling a queued job means it never starts. Cancelling a running job asks
Hermes to stop the run before the API stops listening to it. When the API starts, it fails every
job a previous process left unfinished ("The API restarted before this job finished; please
retry.") and stops its Hermes run. Finished jobs are deleted after `JOB_RETENTION_SECONDS`.

The queue and the cancel signals live in the process, so run **one** uvicorn worker. Jobs, events
and receipts are in SQLite (WAL) at `API_DB_PATH`; all database work runs in threads.

On `SIGTERM` uvicorn waits at most 3 s (`--timeout-graceful-shutdown` in the `Dockerfile`) for open
event streams, which browsers resume from their cursor. Then the runner fails live jobs and asks
Hermes to stop their runs, waiting up to 10 s. The container therefore needs a stop grace of about
30 s (`stop_grace_period` in `compose.yaml`) before Docker kills it.

## Routes

| Route | Purpose |
|---|---|
| `GET /health` | 200 while the store answers and the job workers run |
| `GET /v1/pack` | Title, disclaimer, questions and the example picker's questions (`examples`) of the active pack |
| `GET /v1/data_sources` | Sources the running tools can serve, with their capabilities |
| `GET /v1/data_sources/{id}/schema` | Tables, views, columns, keys and relationships |
| `GET /v1/data_sources/{id}/preview?table=&limit=` | First rows (at most 100) |
| `POST /v1/data_sources/{id}/query` `{sql}` | One SELECT over the database, in a separate process: 5 s, 100 rows, 2 at a time |
| `GET /v1/data_sources/{id}/ontology` | Auto Ontology graph (ontology profile; 404 otherwise) |
| `POST /v1/jobs/async/submit` | `{input, data_sources?, job_id?}`, header `conversation-id` → `{job_id, status}` |
| `GET /v1/jobs/async/job/{id}` | `{job_id, status, error, created_at}` |
| `GET /v1/jobs/async/job/{id}/report` | `{job_id, has_report, report}` |
| `POST /v1/jobs/async/job/{id}/cancel` | `{job_id, status: "interrupted", cancelled: true}`; 409 once finished |
| `GET /v1/jobs/async/job/{id}/stream[/{cursor}]` | Server-Sent Events (below) |
| `GET /v1/jobs/async/job/{id}/trace` | `{trace_id, path}`: the job's trace in Phoenix |
| `GET /v1/jobs/async/job/{id}/export` | The job as one turn of the recordings bundle |
| `POST /v1/jobs/async/job/{id}/benchmark` | Compare the finished job's market calls on the CPU and the GPU ([below](#benchmark)); a stored comparison is returned as is |
| `GET /v1/jobs/async/job/{id}/benchmark` | The stored comparison; 404 until one has run |
| `GET /v1/jobs/async/job/{id}/retrieval-benchmark` | The Milvus CPU/GPU index comparison that applies to the finished job's retrieval calls ([below](#benchmark)); 404 on a CPU-only stack |
| `POST /v1/speech/transcriptions` | One 16 kHz mono PCM16 WAV (`audio/wav`, at most 3 MiB) → `{text}` ([below](#voice-input)) |
| `GET /internal/hermes/jobs/{id}/execution-scope` | Plugin: the job's sources, database, collection and model tiers |
| `POST /internal/hermes/jobs/{id}/tool-receipts` | Plugin: one `ReceiptV2` |
| `POST /internal/hermes/jobs/{id}/llm-calls` | Plugin: one model call, with the served model and its tier |

Unknown jobs, and jobs deleted by retention, are 404. `/internal/hermes/**` needs the `X-Receipt-Key`
header.

### The event stream

Stored events are sent with their store cursor as the SSE `id:`; frames the stream makes up
(`stream.start`, `stream.mode`, `job.status`) have no id, so a browser that reconnects with
`Last-Event-ID` never skips a stored event. The stream replays stored events, sends
`stream.mode: live`, polls every 0.5 s (with a keepalive comment every 15 s), and ends with a
`job.status` frame once the job is finished (`reconnected: true` on a resumed stream). Execution
events are `execution.v2` (`contracts/README.md`); the answer is an `artifact.update` with
`output_category: final_report`.

## Benchmark

The Benchmark tab compares a finished job's market analytics calls on the CPU and the NVIDIA GPU
(`src/demo_api/benchmark/`, contract `contracts/schemas/benchmark.schema.json`). For each call that
returned a result, in order, the API sends the arguments its receipt recorded to the market-analytics
service's `POST /benchmark` (`tools/market-analytics/README.md`). There the GPU worker and a CPU worker
run the same MCP tool: once each untimed, then in matched pairs that alternate which engine goes
first, until `BENCHMARK_PAIRS` pairs or `BENCHMARK_BUDGET_SECONDS` per call. A trial's time is the
tool's own compute timer, the one receipts show; the agent is not rerun. The service compares the
two payloads (floats within 1e-4).

A stage claims a speedup (`qualified`, `speedup` = median CPU / median GPU, and the range of the pair
ratios) only when the payloads matched, at least 5 pairs ran, and the GPU was faster in every pair;
otherwise the medians are shown with no claim. One comparison runs at a time (429 otherwise); a
running job is 409, and a job without market calls 422. On the CPU-only `analytics` profile, or with
no analytics service, the answer is `status: "unavailable"` with the reason, and nothing is stored;
a completed or failed comparison is stored with the job, which `export` then carries.

**Milvus.** On a GPU host (analytics-gpu with retrieval) the `retrieval-benchmark` one-shot measures each index
build once: the pack's held-out queries on the CPU index and on a `GPU_IVF_FLAT` copy in a GPU Milvus
([retrieval](../docs/retrieval.md#cpugpu-index-comparison-analytics-gpu)), into
`/data/active/retrieval-benchmark.json` (`src/demo_api/benchmark/retrieval.py`, contract
`contracts/schemas/retrieval-benchmark.schema.json`). `GET .../retrieval-benchmark` returns it for a finished job
whose retrieval calls searched that build (their receipts' `collectionVersion`): 404 when the stack has none, 409
while the job runs or once the index was rebuilt, 422 when the job searched no documents. `export` carries it as
`retrievalBenchmark`, or null.

## Voice input

`POST /v1/speech/transcriptions` (`src/demo_api/speech/`) checks the WAV (16 kHz, mono, 16-bit, at most
`SPEECH_INPUT_MAX_SECONDS`), streams the PCM in 100 ms chunks to NVIDIA Nemotron ASR on
build.nvidia.com (`grpc.nvcf.nvidia.com:443`, the hosted `nemotron-speech-streaming-en-0.6b`
function) with the `nvidia-riva-client` gRPC client, and returns only the final transcript. Two
transcriptions run at a time; a third waits at most a second, then gets 429. Provider errors become
short, fixed messages (503 not authorized or not configured, 504 timeout, 422 no speech, 502
otherwise). With `SPEECH_CLEANUP_MODEL` set, one chat completion with that public model on
build.nvidia.com, with the same key, deletes fillers and false starts; its answer is kept only if
every word of it appears, in order, in the transcript, and any failure keeps the transcript as is.
Voice input is off unless `SPEECH_INPUT_ENABLED` is true and `SPEECH_API_KEY` is set (503).

## Recordings

`demo-api record` asks the pack's featured questions (`--all` for every question and conversation,
`--question ID` for some) on a running stack and writes the bundle the UI replays. A question is a
session of one turn; a conversation (`conversations` in `questions.yaml`) is one session whose turns
are asked in order with one `conversation-id`, so each sees the answers before it:

```text
data/packs/<pack>/recordings/
  index.json          {schemaVersion: 2, pack: {id, version}, recordedAt, sessions: [{id, title, featured, turns: [{jobId, question}], tools}]}
  pack.json           copy of /data/active/pack.json
  sessions/<id>.json  {schemaVersion: 2, id, title, turns: [<export>]}
  database.json       {schemaVersion: 1, sources: [{id, name, databaseName, schema, previews, queries: [{sql, result}]}]}
```

Each `index.json` session also lists the `tools` its runs used, `[{pill, device, tools}]`
(`src/demo_api/pills.py`): one per technology pill, with the engine a market tool reported (`gpu` or `cpu`) and
the tool ids behind it, for the UI's replays list.

An export turn is `{jobId, question, submittedAt, completedAt, status, report: {markdown, citations[]} | null,
events: [execution.v2], receipts: [ReceiptV2], sourceIds, benchmark, retrievalBenchmark}`. After each answer the
recorder asks for its CPU/GPU comparison, so a bundle recorded on the GPU profile replays the
Benchmark tab (`benchmark` and `retrievalBenchmark` stay null on the CPU profile). `database.json` lets the data viewer work
in replay: each structured source's `GET .../schema`, the first 8 rows of each table
(`GET .../preview`), and the `POST .../query` results of the SQL the recorded answers ran and of the
viewer's starting query for each table. `demo-api snapshot-database --out <recordings>` rewrites only
`database.json`. `scripts/demo.sh record` runs the command in
the api image on the stack's network, where the defaults (`--api-url http://api:8000`,
`DATA_ACTIVE_DIR=/data/active`) are right. A question or conversation that does not succeed is left
out, and the command exits 1. `--question` updates only the named sessions and keeps the rest of the
bundle; a whole set (the featured questions, or `--all`) replaces it.

## Environment

Secrets can also be files in `/run/secrets` named after the setting (Compose secrets).

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_URL` | `http://hermes-gateway:8642` | Hermes Runs API |
| `HERMES_API_SERVER_KEY` | – (secret) | Bearer key of the Hermes API server |
| `HERMES_RECEIPT_API_KEY` | – (secret) | `X-Receipt-Key` of the internal routes |
| `AGENT_FEATURES` | `retrieval,analytics` | Tool groups in the agent image (as its build argument); limits capabilities and toolsets |
| `AGENT_EFFICIENT_MODEL`, `AGENT_CAPABLE_MODEL` | empty | Switchyard's model ids, for the tier of each model call |
| `DATA_ACTIVE_DIR` | `/data/active` | The built data pack |
| `API_DB_PATH` | `/var/lib/demo-api/jobs.db` | SQLite job store |
| `JOB_MAX_ACTIVE`, `JOB_MAX_QUEUED` | `1`, `4` | Queue size |
| `JOB_DEADLINE_SECONDS` | `1200` | Per-job backstop deadline |
| `JOB_RETENTION_SECONDS` | `86400` | How long finished jobs are kept |
| `HERMES_RUN_WALL_TIMEOUT_SECONDS` | `900` | Hermes run deadline |
| `HERMES_RUN_IDLE_TIMEOUT_SECONDS`, `HERMES_RUN_NO_PROGRESS_TIMEOUT_SECONDS` | `600`, `600` | Progress budgets; a judged model reply can stay silent for minutes |
| `HERMES_RUN_MAX_TOOL_CALLS`, `HERMES_RUN_MAX_DUPLICATE_EVENTS` | `128`, `64` | Progress budgets |
| `HERMES_RUN_STOP_GRACE_SECONDS`, `HERMES_RUN_POLL_INTERVAL_SECONDS` | `30`, `1` | Stopping and polling a run |
| `HERMES_HEARTBEAT_SECONDS` | `15` | Progress heartbeat while a run is silent |
| `HERMES_RECEIPT_SETTLE_SECONDS` | `2` (at most 5) | Wait for the last receipts |
| `AIQ_PHOENIX_INTERNAL_URL`, `PHOENIX_PROJECT` | `http://phoenix:6006`, `market-analysis-agent` | Trace lookup |
| `AUTO_ONTOLOGY_URL`, `AUTO_ONTOLOGY_EMAIL`, `AUTO_ONTOLOGY_PASSWORD` | empty | Ontology view; an empty URL turns it off |
| `AUTO_ONTOLOGY_ORIGIN` | `AUTO_ONTOLOGY_URL` | The `Origin` Auto Ontology's sign-in trusts |
| `MARKET_ANALYTICS_URL` | `http://market-analytics:3010` | The market-analytics service, for the Benchmark tab |
| `BENCHMARK_PAIRS`, `BENCHMARK_BUDGET_SECONDS` | `5`, `90` | Matched pairs asked of each call, and the time after which no new pair starts. A call claims a speedup only after 5 pairs, so the budget must fit 5 pairs of the slowest call: 5 pairs of a 500-stock minute-bar scan take about 55 s, and a whole-market one (73 s on the CPU) would need about 450 s |
| `SPEECH_INPUT_ENABLED` | `false` | Voice input |
| `SPEECH_API_KEY` | – (secret) | An nvapi- key for build.nvidia.com; `demo.sh` uses `RETRIEVER_API_KEY` when this is empty and the retriever is build.nvidia.com |
| `SPEECH_INPUT_MAX_SECONDS`, `SPEECH_MAX_CONCURRENT` | `60` (1 to 90), `2` | The longest recording, and transcriptions at once |
| `SPEECH_ASR_SERVER`, `SPEECH_ASR_FUNCTION_ID`, `SPEECH_ASR_LANGUAGE`, `SPEECH_ASR_TIMEOUT_SECONDS` | `grpc.nvcf.nvidia.com:443`, the hosted Nemotron ASR function, `en-US`, `60` | The ASR endpoint |
| `SPEECH_CLEANUP_MODEL` | empty | A public model on build.nvidia.com for the deletion-only cleanup, e.g. `nvidia/nemotron-3-super-120b-a12b`; empty means none |
| `TOOL_REGISTRY_FILE` | `contracts/tool-registry.json` | Set by the image |

## Run

```bash
docker build --build-context contracts=contracts -t market-demo/api:local api
scripts/demo.sh record                            # record the featured questions on the running stack
```

The job runner keeps the semantics the prototype got from Dask and NAT: order, the queue cap,
cancelling queued and running jobs, the deadline, progress budgets, restart recovery, shutdown and
retention. The contract models in `src/demo_api/events/`, `receipts/` and `benchmark/`
feed `scripts/gen-contracts.sh`.
