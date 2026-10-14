<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Retrieval

Document questions go to one MCP tool, `retrieve_evidence`, served by `tools/retrieval`. It embeds and
reranks with NVIDIA Nemotron retrieval models through LangChain's NVIDIA partner package, and stores vectors
in Milvus through plain `pymilvus`. There is no LlamaIndex, no LangChain agent and no LangGraph: Hermes is the
only agent, and LangChain is used only for its NVIDIA embed and rerank clients.

| Piece | Version |
|---|---|
| `langchain-nvidia-ai-endpoints` (`NVIDIAEmbeddings`, `NVIDIARerank`) | 1.4.3, exact pin |
| `langchain-text-splitters` (`RecursiveCharacterTextSplitter`) | 1.x |
| `pymilvus` | 2.6.17 |
| Milvus | 2.6.25, CPU standalone: embedded etcd, local storage, no MinIO. The analytics-gpu profile adds a second, GPU standalone (`v2.6.25-gpu`) for the [CPU/GPU index comparison](#cpugpu-index-comparison-analytics-gpu) only |
| Embed model | `nvidia/nemotron-3-embed-1b` |
| Rerank model | `nvidia/llama-nemotron-rerank-vl-1b-v2` |

The component's README, [`tools/retrieval/README.md`](../tools/retrieval/README.md), has the environment, the
data contract and the result schema.

## Pipeline

**Index (`retrieval-index`, a one-shot under the retrieval profile).**
1. Read `/data/active/corpus/documents.jsonl`, which `data-corpus` builds from the pack's corpora.
2. Split each document into chunks of up to 2,400 characters with a 240-character overlap.
3. Embed the chunks as passages, 50 per request.
4. Load them into a new Milvus collection named `<alias>__<fingerprint>`, point the alias at it when it is
   complete, drop older builds, and write `/data/active/collection-manifest.json`.

The fingerprint covers the corpus bytes, the embed URL and model, the chunking and the index parameters.
Re-running the index on unchanged input does nothing, and a changed input builds a new collection while the
server keeps answering from the previous one. The default pack indexes about 5,200 documents
(SEC EDGAR filings and eCFR Title 17) as about 23,600 chunks.

**Search (`retrieval`, `127.0.0.1:8120/mcp`).** `retrieve_evidence(query, source_ids, top_k=8)`; a `top_k` above
8 (the schema allows 25) returns 8:
1. Embed the query once.
2. Search every selected source for the same share of candidates, `min(4 × top_k, 200 ÷ sources)`, in the
   build the alias points at (resolved once per call).
3. Rerank all candidates together in one request and return the best `top_k` passages with their title, URL,
   date, scores and metadata, plus the models, the index settings and timings. A result longer than 30,000
   characters as the agent reads it drops its lowest-ranked passages ([result size](#result-size)).

The Hermes plugin sets `source_ids` to the job's selected document sources, and the tool refuses any source
the pack does not declare as documents. Each call becomes a `retrieval_evidence` receipt, which the UI's
retrieval explorer shows.

**Queries over filings.** The first passage of every 8-K and 6-K is its cover page: the form name, "current
report", the registrant and the dates. A query that names the form ("second quarter 2026 operational disruption
8-K") is nearest to those cover pages, and the reranker scores them above the passages that describe an event,
so it returns cover pages only: on the synthetic-market sample, none of its 8 passages comes from a filing that
reports a disruption, against 3 of 8 for the same query without "8-K". The tool description and the
`searching-documents` skill therefore tell the agent to leave form names out and to write the event as the
sentence a filing would contain: a list of keywords matches the risk lists of forward-looking statements
instead. Each hit's metadata carries its form and filing date.

## Endpoints

Retrieval uses the retriever endpoint (`RETRIEVER_*`), never the inference endpoint. Rerank requests go to the
first of these that applies:

1. `RETRIEVER_RERANK_URL`, if set (for example a self-hosted NIM's `http://<host>:8000/v1/ranking`);
2. the model's hosted endpoint, when the base URL is build.nvidia.com (the default);
3. `{RETRIEVER_BASE_URL}/ranking`, the self-hosted NIM path.

An OpenAI-compatible gateway that serves the agent's models may have no route for the VL reranker, which is
why the retriever has its own endpoint and defaults to build.nvidia.com whatever serves the agent's models.

## Documented configuration items

The integration has no code workarounds: no subclass, no private attribute, no monkeypatch. It needs these
configuration choices, all through public API:

| # | Choice | Why | Where |
|---|---|---|---|
| 1 | Register `nvidia/nemotron-3-embed-1b` with `register_model` | 1.4.3's model table does not list it. Without an entry, constructing the client runs a blocking `GET /v1/models` lookup | `nvidia.py` |
| 2 | Pass `base_url` explicitly to every client | The package's default comes from `NVIDIA_BASE_URL`, which would leak in from other software. This repository never uses `NVIDIA_BASE_URL` | `nvidia.py` |
| 3 | Pass `api_key` explicitly, and refuse to start without one | An empty key falls back to `NVIDIA_API_KEY`. That is also why the `.env` names are `INFERENCE_*` and `RETRIEVER_*` | `nvidia.py`, `settings.py` |
| 4 | Rerank with `top_n = max_batch_size = 200` | Search never collects more than 200 candidates, so reranking is always one request (the default batch of 32 would split it) | `nvidia.py` |
| 5 | Accept the clients' fixed 60 s timeout | 1.4.3 ignores a `timeout` argument | – |
| 6 | Retry transient failures around every embed and rerank call | The clients have no retries. A tool call gets 3 attempts; the index build gets 8 with 0.5–8 s of jittered backoff. Dropped connections, timeouts, 408, 429 and 5xx are retried, including the async client's `[###] Unknown Error` for a non-JSON error page | `nvidia.py` |
| 7 | Emit three OpenInference spans: `embed`, `search`, `rerank` | LangChain's instrumentation emits nothing for direct embed and rerank calls | `search.py` |
| 8 | Pin `==1.4.3` and set `NVIDIA_USAGE_TELEMETRY_ENABLED=false` | The next release turns usage telemetry on by default; 1.4.3 is also the floor for GHSA-g28h-2cmm-rj9x | `pyproject.toml`, `__init__.py` |
| 9 | Reserve the document metadata key `image` | `NVIDIARerank` would send it to the reranker as an image. The index build rejects documents that use it | `datapack.py` |

Paths are under `tools/retrieval/src/demo_retrieval/`. Items 1 and 5 can go once the package lists the model
and honours `timeout`.

Two related details:
- Both clients set `truncate="END"`, so an over-long passage is cut rather than rejected.
- With `RETRIEVER_RERANK_URL` set, the reranker is registered with that URL and built with the build.nvidia.com
  base URL, because the client honours a registered endpoint only in hosted mode. No request goes to the base
  URL itself.

**Milvus.** An explicit schema: `chunk_id` primary key; `source_id` as the partition key; `document_id`,
`title`, `url`, `published_at` and `text` columns; an `embedding` vector; dynamic fields for each source's own
metadata. The index is HNSW with cosine similarity, `M = 16`, `efConstruction = 200`, and searches use
`ef = 128`, above the largest per-source candidate count (100).

## CPU/GPU index comparison (analytics-gpu)

On a GPU host the Benchmark tab compares Milvus vector search on the CPU index with an NVIDIA GPU index of the
same vectors, as the original demo did. Answers never change: `retrieve_evidence` searches the CPU index above
on every host, and the GPU index exists only for this comparison.

- **Where.** The analytics-gpu profile adds `milvus-gpu`, Milvus on the GPU image (embedded etcd, local
  storage, its own volume, no host port), and `retrieval-benchmark`, a one-shot that `demo.sh up` (and `data
  reindex`) runs once the stack is up, with the retrieval profile. Nothing waits on either: if `milvus-gpu` does not
  turn healthy, `up` warns, stops it and goes on. Without the profiles neither runs, and the Benchmark tab says the
  stack runs the CPU index only.
- **The GPU index.** The one-shot reads the active build's chunk ids, sources and vectors from `milvus`, and
  writes them, L2-normalized, to a collection of the same name in `milvus-gpu` under `GPU_IVF_FLAT` (NVIDIA
  cuVS IVF-Flat, as the original demo used: inner product, `nlist = 128`; searches probe `nprobe = 64` lists).
  Inner product on normalized vectors ranks as cosine does on the CPU. Older builds' copies are dropped, so the
  GPU holds one, and a copy under another index is rebuilt. `GPU_CAGRA` was tried first and returned wrong
  neighbors on these 2,048-dimension vectors in Milvus 2.6.25 on an A100 (recall 0, with IVF-PQ or NN-descent
  graph builds and with either metric), while `GPU_IVF_FLAT` and `GPU_BRUTE_FORCE` matched the exact neighbors.
  Measured on 2026-10-01 on an A100 with a 32,676-chunk SEC and eCFR corpus (its 15 held-out queries): recall@10 0.98
  on both indexes and a CPU/GPU overlap of 1.0 in every profile, and the GPU searched 1.13x faster one query at
  a time, 1.11x in batches of five and 1.76x with five concurrent requests. The GPU Milvus then held 4.7 GiB. `tools/retrieval/milvus/gpu.yaml` caps Milvus's GPU
  memory pool (1 GiB at start, 4 GiB at most).
- **The workload.** The pack's held-out queries (`documents.benchmark_queries` in `pack.yaml`, 15 per pack,
  never the demo questions), embedded once per build with Nemotron Embed. Their vectors are kept beside the
  manifest (`/data/active/retrieval-benchmark-queries.npz`, keyed by the collection build, the embed model and the
  query texts), so every measurement of a build searches the same vectors. Three profiles: one query per request,
  batches of five query vectors of one source scope, and five concurrent requests. Each profile warms both
  indexes up, then sends every request three times, alternating which index goes first. Only the Milvus search
  call is timed (top 10, filtered to the query's sources); embedding and reranking are left out.
- **Quality gates.** Recall@10 of each index against the exact inner-product neighbors, computed from the same
  vectors: at least 0.95 on average and 0.80 for every query. The two indexes' results overlap by at least
  0.95 on average (Jaccard), and each returns the same neighbors in every repetition. Neighbors compare by
  their exact score, taken from one product of the query with every vector, so a chunk scores the same number as
  a true neighbor and as a result. Scores within 1e-6 are one neighbor, so chunks with identical vectors
  (boilerplate repeated across filings) count as one, whichever copy an index returns.
- **Claim.** A profile claims a GPU speedup only when its gates pass and the CPU's total search time is at
  least 1.1 times the GPU's; otherwise it reports the ratio without a claim.
- **Result.** `/data/active/retrieval-benchmark.json` (contract `RetrievalBenchmark`,
  [contracts](../contracts/README.md)), written only by the one-shot. The API serves it as `GET
  /v1/jobs/async/job/{id}/retrieval-benchmark` for a run whose retrieval calls searched the same build, and a job
  export, so a recording, carries it. An unchanged build is not measured again, unless its GPU copy was built
  under another index, or its result has no query vectors beside it (measured by an earlier version, which the
  next `up` measures again); `data reindex` measures a new one. The one-shot never fails the stack: a problem is
  logged (`demo.sh logs retrieval-benchmark`), and the tab shows no comparison.
- **A second measurement.** `demo-retrieval benchmark --guard` measures the build once more, on the same GPU copy
  and query vectors, into `/data/active/retrieval-benchmark-guard.json`. It writes nothing else: whatever it
  measures, the Benchmark tab keeps the comparison measured at `up`.

### Why recall moved between runs

On an A100 with that corpus, four measurements of the same index measured CPU (HNSW) recall@10 of 0.94, 0.94,
0.9733 and 0.9733, and GPU (`GPU_IVF_FLAT`) recall of 0.9467, 0.9467, 0.98 and 0.98, while the overlap of the two
indexes stayed at 0.9879. Both indexes gained or lost the same 5 of 150 neighbors together, which points at the
measurement, not at either index. Two things combined:

- **The query vectors changed on every run.** The measurement embedded the queries again each time, and the hosted
  embed model returns a slightly different vector for the same text on most calls: over five embeddings of the 17
  benchmark queries of both packs, 0 to 4 of the 17 were bit-identical to the first, with components differing by
  up to 7e-8 (cosine similarity at least 0.9999998).
- **The truth and the results were scored by two different computations.** The exact neighbors came from a float32
  matrix product over the query's sources, each returned chunk from its own float32 dot product, and both were
  rounded to six decimals before they were compared. The two disagreed on about 0.5% of the scores (1,062 to
  1,129 of 212,814 per embedding below), so a true neighbor that an index did return could count as a miss, and
  which ones did changed with the last bits of each new query vector. Chunks with identical vectors share one
  score, so one disagreement can remove every copy at once, from both indexes' recall. The overlap compares the two
  indexes' results with each other, scored the same way, which is why it did not move.

Measured locally on a CPU build of the same embed model (23,646 chunks of SEC current reports and
eCFR sections; the 15 benchmark queries those sources cover, embedded five times; the GPU index's `nlist = 128`
and `nprobe = 64` on a CPU `IVF_FLAT` copy):

| Recall@10 over five embeddings | Before | Now |
|---|---|---|
| HNSW, `ef = 128` (what `retrieve_evidence` uses) | 0.9733 to 0.9933 | 1.0 every time |
| HNSW, `ef = 256` or `512` | the same as `ef = 128` | 1.0 every time |
| IVF_FLAT, `nprobe = 64` | 0.9667 to 0.9867 | 0.9933 every time (one neighbor missed) |

The overlap was 0.9879 every time. Raising `ef` changed nothing, because HNSW already returned every exact
neighbor, so the search parameters stay as they are and answers are unchanged; the gates stay at 0.95. The 1e-6
tolerance only joins identical vectors and float32 rounding: in that build no two of any query's 30 best scores
were within 1e-6 of each other (2,175 gaps over the five embeddings, 5 of them under 1e-5).

## Result size

A passage is one chunk, at most 2,400 characters, and it is never cut shorter: the fact a question needs can sit
at a chunk's end. Each hit adds about 1 KB of ids, URL and metadata. The MCP SDK sends a result twice
(structured content and the same JSON as text), and Hermes gives the model one copy, inside a JSON string.

Hermes hides an MCP result longer than 50,000 characters from the model ([tool result
size](architecture.md#tool-result-size)), so a call returns at most 8 passages and drops its lowest-ranked ones
while it is longer than 30,000 characters as the agent reads it. The longest eight eCFR chunks of a
full corpus come to 29,100 characters; eight 2,400-character SEC filing passages, 28,200.

| `top_k` | As the agent reads it | JSON-RPC response |
|---|---|---|
| 3 | 11 KB | 21 KB |
| 8 (default and most) | 27 KB, 30 KB at most | 53 KB |

OpenShell 0.1.2 caps MCP JSON-RPC request bodies at 64 KiB, not responses, so these pass. The receipt cuts
each passage to 1,500 characters for the UI.

## Operating it

```bash
./scripts/demo.sh data reindex    # rebuild the index, e.g. after changing the embed model or base URL
./scripts/demo.sh logs retrieval  # the server's log
```

A reindex builds a new collection and moves the alias only when it is complete, so the running server keeps
answering. The spans appear under the job's trace in Phoenix ([operations](operations.md#phoenix)).
