<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Data packs

A data pack is one directory under `data/packs/` that describes a whole demo world: its tables, its document
corpora, the sources the UI and the agent see, the analytics and prediction settings, the demo questions and
the recorded sessions. The `demo-data` builder turns the selected pack into files under `/data` and points
`/data/active` at the result. Services read only `/data/active`, so swapping the data means adding a pack, not
changing code.

The format, the runtime layout and the builder's commands are specified in
[`data/README.md`](../data/README.md). How external data is fetched, imported and scaled is in
[data platform](data-platform.md). This page is the guide.

## The pack

The repository ships one pack, [`synthetic-market`](../data/packs/synthetic-market/README.md), the default. The
format also takes a pack of your own built from a real market dataset that stays outside the repository: the
same tables, built by the same importer ([data platform](data-platform.md)). In every pack, SEC EDGAR filings
are a separate document source, searched by retrieval. They are never converted into news events or written to
a news table: the demo shows the agent combining a price database and a document corpus, each through its own
tool.

| | `synthetic-market` |
|---|---|
| Prices | Fictional issuers (2,000 at the default `standard` profile), seeded prices; one-minute bars in the `ci` and `intraday` profiles |
| Company data | Names and profiles written by Nemotron with NeMo Data Designer, checked against SEC's ticker lists |
| Ticker-linked news | `company_news`: seeded events with Nemotron headlines, and 12 planted stories |
| `sec_filings` | 1,000 real 8-K and 6-K filings from 2026 Q2, by any filer (the issuers are fictional) |
| Other documents | `market_regulations`: eCFR Title 17 and the SEC's 2023 cybersecurity disclosure rule (Form 8-K Item 1.05 and its deadline) |
| Needs | Nothing for the structured part; `SEC_USER_AGENT` for `sec_filings` |

The synthetic market has planted facts, so answers can be checked against a build. The downloaded documents are
fetched at build time from pinned URLs and checked against their SHA-256; they are never committed. A pack that
declares an external dataset gets its data through a fetch step first:

```bash
# .env: DATA_PACK=<your pack>, and DATA_SOURCE_<ID>=<a directory, host:/path or URL>
./scripts/demo.sh data fetch          # into $DATA_SOURCE_DIR/<id>, verified against the pinned manifest
./scripts/demo.sh up                  # builds the pack; on a running stack, also switches everything to it
```

A fresh clone builds and replays `synthetic-market` with nothing to fetch.

## How a pack is built

```text
./scripts/demo.sh up                  # runs the data one-shots as dependencies
./scripts/demo.sh data prepare        # rebuild the active pack, e.g. after changing its files or DATA_CORPORA
```

| Step | Service | Writes |
|---|---|---|
| Structured part | `data` (core) | `tables/*.parquet`, `structured/<database>.duckdb`, `ontology/model.yaml`, `prediction/`, `pack.json` |
| Corpus | `data-corpus` (retrieval) | `corpus/documents.jsonl` |
| Index | `retrieval-index` (retrieval) | the Milvus collection and `collection-manifest.json` |

Each build lives in `/data/builds/<pack>@<version>+<profile>+<digest>`. The digest covers the pack's files
(not its `README.md` or `recordings/`), the profile, the selected corpora and the builder
itself, so preparing an unchanged pack is a no-op and any change starts a new build. `/data/active` switches
atomically. `data prepare` also restarts market analytics and retrieval, which keep the build they resolved at
startup, and recreates the sandbox when the new build changes the tools' schemas (Hermes lists them once, when
the sandbox starts). After changing `DATA_PACK` or `DATA_PACK_PROFILE` on a running stack, run
`./scripts/demo.sh up`, which does all of that and also recreates what names the pack's database.

`pack.json` in the build is what services read: the sources and questions this build can serve, the database
name and paths, the analytics and prediction settings, the minute bars' location, and the digests of every file.

Settings (`.env`, [configuration](configuration.md#4-data-pack)): `DATA_PACK`, `DATA_PACK_PROFILE`,
`DATA_CORPORA` and `SEC_USER_AGENT`. An empty `DATA_CORPORA` builds every corpus the pack does not mark `opt_in`:
`sec_filings` and `market_regulations` in `synthetic-market`. An `opt_in` corpus, such as GDELT headlines in a pack
with an external dataset, is built only when named.

Other commands, all run in the data image:

```bash
./scripts/demo.sh data validate       # the pack's schema, cross-references and tool contracts
./scripts/demo.sh data verify         # the active build still matches the digests in its pack.json
./scripts/demo.sh data list           # packs and builds
./scripts/demo.sh data clean [--all]  # remove inactive builds and unused caches (--all: every cache)
./scripts/demo.sh data fetch          # fetch and verify the pack's external datasets
./scripts/demo.sh data generate       # write synthetic-market's Nemotron text (rarely; needs a key)
./scripts/demo.sh data reindex        # rebuild only the retrieval index
```

## Sources, capabilities and questions

Each source in `pack.yaml` declares its `capabilities`, which are tool families from
`contracts/tool-registry.json`: `unstructured_retrieval`, `market_analytics`, `structured_retrieval`,
`structured_prediction`. The API offers a source with only the capabilities the running tools provide, and a
job's selected sources decide which tools the agent gets. For example, `market_data` offers
`structured_retrieval` only when the ontology profile runs.

A tool whose data a pack lacks stays registered, so the registry, the sandbox policy and the UI never change with
the pack. Its MCP description starts with "Unavailable in the active data pack", and a call fails at once with
`news_unavailable` (no ticker-linked news table) or `minute_bars_unavailable` (no minute bars, as in
`synthetic-market`'s daily-bar profiles). The market skill tells the agent not to call it.

`questions.yaml` holds each pack's demo questions. A question is offered only when every source it names is in
the build and, if it lists `profiles`, the build uses one of them; the API also leaves out a question whose
declared `tools` the running stack lacks, such as `kumo` without a [Kumo service](kumo-service.md). Each declares the `tools` it is expected to use
as technology pills, which the composer's demo scenario picker shows beside its label: `cudf` for any market tool,
plus `cuml` for `market_anomaly_scan` and `cugraph` for `analyze_market_relationships`; `kumo`
(`predict_asset_outcomes`), `retrieval` (`retrieve_evidence`, one pill whatever the sources) and `ontology`
(`ask_question`). The names are the tool registry's `pills` ([contracts](../contracts/README.md#tool-registry)), and
a recorded answer is expected to use every tool its question declares. `tag` (ANALYTICS, RETRIEVAL, ...) only groups questions in the docs. Six are `featured`: they need
only the default profiles, fit the landing page on one screen, and are what `record` asks by default.

`examples` in `questions.yaml` lists, in order, the questions the composer's "Choose an example" picker offers:
at most 12, every featured question among them, and together declaring every tool the pack's questions declare
(`data validate` checks all three). The picker shows five rows and scrolls for the rest, so the first five
should show the widest mix of tools. A build offers the examples it serves (their sources built, their profiles
matching), and `GET /v1/pack` those whose sources the running tools serve. A pack without the list offers its
featured questions, then the others, up to 12. Every question stays in the pack for the landing page links and
`record`, whether or not it is an example. `synthetic-market` lists all eleven.

`questions.yaml` can also hold `conversations`: two to six turns asked in order in one conversation, so a later
turn can refer to an earlier answer ("For those same two examples, ..."). They follow the same source and profile
rules, share the questions' ids, are not listed in the UI, and are recorded by `record --all`, each as one replay
session.

`documents.benchmark_queries` in `pack.yaml` lists held-out queries for the Benchmark tab's CPU/GPU Milvus
comparison on a GPU host, each with the document sources it searches; they are never demo questions. A build keeps
those whose sources it indexed ([retrieval](retrieval.md#cpugpu-index-comparison-analytics-gpu)). `synthetic-market` has 15.

## Recordings

The replay bundle lives with its pack, in `data/packs/<pack>/recordings/`, and is committed. It is written by:

```bash
./scripts/demo.sh record                          # the featured questions
./scripts/demo.sh record --all                    # every question and conversation the build offers
./scripts/demo.sh record --question market-leaders --question peer-network
```

`record` asks each question on the running stack, one at a time, and each conversation's turns in order in one
conversation. A whole set (the featured questions, or `--all`) replaces the bundle; `--question` re-records only
the named questions or conversations and keeps the bundle's other sessions. A question or conversation that does
not succeed is left out (a re-recorded one keeps its earlier session), and the command exits 1. Review the files before committing: they hold questions, answers, evidence
excerpts and model names. `./scripts/demo.sh replay` then serves the UI on the bundle alone, and stops with a
hint when the pack has none. The format is in [`api/README.md`](../api/README.md#recordings).

The replays list shows, under each recorded session, pills for the tools its runs actually used (the union over
its turns). `record` writes them into `index.json`; for a bundle recorded before, the UI derives them from the
recorded events. A market tool that ran on the CPU shows its CPU library (pandas, scikit-learn or NetworkX) in the
same RAPIDS color, not the GPU name.

`synthetic-market`'s bundle holds the ten questions of its default (`standard`) profile. Its eleventh
question, `intraday-ranges`, needs a minute-bar profile (`ci` or `intraday`), so the bundle leaves it out.

```bash
./scripts/demo.sh replay                          # synthetic-market, the default
```

It was recorded on 2026-10-01, and some of its sessions again on 2026-10-02 (the pack's README names them), on
an A100 VM with every profile, including `ontology`, Kumo (then a NIM in the demo stack, since moved to its own
[service](kumo-service.md)) and the GPU Milvus. So every answer that called the market tools carries its CPU/GPU
comparison for the Benchmark tab, and every answer that searched documents carries the Milvus CPU/GPU index
comparison (`retrievalBenchmark`). It was recorded with the `.env.example` models and corpora (Nemotron 3 Ultra
alone, on build.nvidia.com), as a public user runs it. The recorder writes served model ids without a gateway's
provider prefix, keeping the last segment of the id the endpoint served. Every answer was reviewed against the
pack's planted facts and evidence; the ones that were wrong were asked again, and the bundle keeps the faults
that remained rather than hiding them (the pack's README lists them).

## Adding or swapping a pack

1. Copy `data/packs/synthetic-market` to `data/packs/<new-id>`. For your own bars, replace its `generator` with
   an `external` dataset pinned by its manifest fingerprint, and describe their layout under `market.bars`
   ([data platform](data-platform.md#pack-format-v2) has a worked example).
2. Edit `pack.yaml` (identity, licenses, provenance, sources, disclaimer, analytics universes, prediction),
   `schema.sql`, `ontology.yaml` and `questions.yaml`. Only the columns in the tool contracts are mandatory.
3. Set `DATA_PACK=<new-id>` in `.env`, then run `./scripts/demo.sh data validate` and
   `./scripts/demo.sh up`. Record its sessions with `./scripts/demo.sh record`.

Auto Ontology answers from the descriptions in `ontology.yaml`. After you edit them, the next `up` rebuilds the
model and seeds the pack's catalog again ([seeding](../tools/auto-ontology/README.md#seeding)).

No code changes are needed while the pack satisfies the contracts its tools own. Market analytics requires what
`tools/market-analytics/contract/market-analytics.v1.json` lists (a pack declares
`analytics.contract: market-analytics/v1`); `validate` checks the declared schema and `prepare` checks the
built tables. The database name is the pack id in snake case.

## Licenses

- The synthetic market data and its Nemotron-written text are Apache-2.0.
- A pack's external dataset keeps its own license; it is never committed or redistributed here.
- eCFR is United States government public information. The eCFR is authoritative but is not the official legal
  edition of the CFR. The Federal Register rule is United States government public information too.
- SEC EDGAR content falls under the SEC's website reuse terms, and issuer-authored content may carry its own
  rights; its redistribution terms are unknown. The filings are fetched at build time and never committed.
