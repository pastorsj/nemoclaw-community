<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Data platform

How the demo's data works: one pack format for synthetic and real markets, external data that is never
committed, a fetch step, an importer that scales past a few gigabytes, the document sources beside the prices,
and the questions the pack answers. [Data packs](data-packs.md) is the guide and
[`data/README.md`](../data/README.md) the reference for the format and the commands.

## Rules

1. **Real market data is never committed.** It lives outside the repository in `DATA_SOURCE_DIR`, reaches
   a machine through `demo.sh data fetch`, and is pinned by a digest. The repository holds only synthetic data.
2. **Scale is a property of the format, not of one dataset.** Nothing loads the raw minute bars whole. The
   importer was built against a real dataset of 1-minute bars (1.7 GB, 117 million bars for 2,200 US tickers,
   not included here), and that size is a test case, not a ceiling.
3. **One format for synthetic and real data.** `synthetic-market` (NeMo Data Designer) is the pack this
   example ships. A pack of your own can declare an external dataset of real bars; both produce the same
   tables through the same importer.
4. **SEC EDGAR filings are a separate document source**, searched by retrieval. They are never converted into
   news events and never written to a news table.
5. **GDELT headlines can be an optional, separate document source** of a pack with an external dataset. They
   are not linked to tickers.
6. **The news tools need a ticker-linked news table.** `synthetic-market` has one. A pack without one
   declares none, and `sentiment_timeline` and `analyze_news_price_relationship` report that they are
   unavailable.
7. **Minute bars are read in place.** `intraday_scan` scans them from the raw dataset, batch by batch, and
   reports that it is unavailable in a pack or profile without them.

## Overview

```text
  source (local, rsync, https, s3, gs, hf)
        │  demo.sh data fetch: manifest digest pinned in pack.yaml, sha256 per file
        ▼
  DATA_SOURCE_DIR/<dataset>/      raw Parquet, read in place, read-only to every service
        │  rollup: one DuckDB pass, streamed, cached by manifest digest
        ▼
  /data/cache/rollups/<key>/      daily bars per symbol
        │  import: sessions, exclusions, companies, peers, news (if any)
        ▼
  /data/builds/<build>/           tables/*.parquet, DuckDB, ontology, prediction
        ▲
  /data/active ──▶ api, market-analytics, Auto Ontology, retrieval (unchanged)
```

`synthetic-market` goes in at the same place. At build time its generator writes a raw dataset in the
canonical layout into `/data/cache/generated/`, and the same rollup and import run on it.

## The packs

| | `synthetic-market` (default) | A pack with an external dataset (your own) |
|---|---|---|
| Prices | Generated from seeds, 1 day or 1 minute bars | Real 1-minute or daily bars, rolled up to daily |
| Issuers | Fictional, named by Nemotron, checked against the SEC ticker list | Real, with SEC names, CIKs and SIC codes (`companies: sec`) or a companies table of its own |
| Ticker-linked news | `company_news`: seeded events, Nemotron headlines | Optional (`market.news`); without it the news tools report unavailable |
| Documents | `sec_filings` (SEC EDGAR 2026 Q2), `market_regulations` (eCFR Title 17 and the 2023 cybersecurity rule's Form 8-K text) | `sec_filings` with its own filings manifest, `market_regulations`, optionally GDELT headlines |
| Committed | Everything, including the Nemotron text for the committed profiles | The pack definition, the dataset digest and the filings manifest only |
| Needs | No key for the structured part; `SEC_USER_AGENT` for `sec_filings` | `data fetch`; `SEC_USER_AGENT` for company metadata and `sec_filings` |

The structured source is `market_data` and the SEC filings source is `sec_filings`, so the name does not
suggest a news table.

## Pack format v2

`schema_version: "2"` adds two sections to v1: `external` (datasets outside the repository) and `market` (how
the importer turns bars into tables). The importer writes every table, so a pack with `structured` needs
`market`; a pack of documents alone may stay at `"1"`. Everything else in the v1 format is kept (`structured`,
`documents`, `sources`, `analytics`, `prediction`, `ontology`, `questions`), with the changes listed below.

An example for a pack of real minute bars (`my-equities` and its values are placeholders):

```yaml
schema_version: "2"
id: my-equities
version: "1.0.0"
as_of: "2026-03-12"

external:
  minute-bars:                          # fetched into $DATA_SOURCE_DIR/minute-bars/
    title: US equities, 1-minute split-adjusted bars
    manifest: benchmark-bundle-manifest.json    # path inside the dataset
    fingerprint: <sha256 hex of the manifest's files>
    bytes: 1812697046                   # for doctor's disk check
    # Fetched from $DATA_SOURCE_MINUTE_BARS (the id, upper-cased, - to _)

provenance:
  - { id: minute-bars, kind: external, synthetic: false, license: Private-dataset }

market:
  bars:
    dataset: minute-bars
    files: market/stocks_1min/*_full_1min_adjsplit.parquet
    symbol_from_path: '([^/]+)_full_1min_adjsplit\.parquet$'   # or symbol_column: symbol
    columns: { time: ts, open: open, high: high, low: low, close: close, volume: volume }
    frequency: 1min                     # 1min | 1d
    timezone: America/New_York          # the zone of the time column's wall-clock values (a tz-aware column is converted to it)
    regular_session: ["09:30", "16:00"] # both ends included
  companies: sec                        # sec | a Parquet path in the dataset
  news: null                            # or a Parquet path in the dataset
  exclude: [warrants, units, rights, preferred]
  min_sessions: 20
  peers: 8                              # declared peers per asset

structured:
  source: market_data
  database_name: my_equities
  schema: schema.sql
  views: [views/prediction.sql]
  tables:                               # the importer's fixed table set
    - { name: assets, origin: minute-bars }
    - { name: ticker_history, origin: minute-bars }
    - { name: trading_sessions, origin: minute-bars }
    - { name: daily_prices, origin: minute-bars }
    - { name: asset_relationships, origin: minute-bars }

analytics:
  news_table: null                      # no ticker-linked news in this pack
  session_close_utc: "21:00"
```

| Key | Meaning |
|---|---|
| `external.<id>` | A dataset outside the repository: its manifest path, its pinned `fingerprint` and its size. The host path is `$DATA_SOURCE_DIR/<id>`, mounted read-only at `/sources/<id>`. |
| `market.bars` | Where the bars are and how to read them in place: a glob, where the symbol comes from (a path pattern or a column), the column mapping, the bar frequency, the time zone of the timestamps and the regular session. |
| `market.companies` | `sec` joins SEC company metadata to the symbols. A path reads a companies table from the dataset. |
| `market.news` | A path to a ticker-linked news table in the dataset, or `null`. |
| `market.exclude`, `min_sessions`, `peers` | Which symbols the importer drops (see [import](#import)), and how many declared peers each asset gets. |
| `analytics.news_table` | May be `null`: it must be set exactly when `market.news` is. |
| `prediction.population` | May name only a `view`. The builder resolves the ids into `pack.json`, so readers still see `ids`. |
| `provenance[].kind: external` | A new provenance kind, next to `generated`, `committed` and `download`. The importer writes every table, each with the `origin` of `market.bars.dataset`: an `external` entry, or in `synthetic-market` the `generated` entry of its generator, whose raw output is cached in `/data/cache/generated/<key>/` and imported the same way. |
| `generator.profiles.<p>.params.frequency` | The bars a generator profile writes (`1min` or `1d`). `pack.json` reports it as `market.bars.frequency`, so `intraday_scan` knows a daily-bar profile has no minute bars. |
| `documents.corpora[].files` | Instead of a `manifest`: a glob inside the corpus's `external` origin, read in place (`gdelt-parquet`), only from files the dataset's manifest lists and `fetch` verified. |
| `license.path` | Now optional: a private dataset may have no public terms. |

`validate` checks that the tables with that origin are exactly the importer's set, and `pack.json` gets
`market.bars` with `root`, the raw dataset's directory as the services see it, for tools that read minute bars.

The build digest already covers `pack.yaml`, so it covers the pinned fingerprints too. A build of a pack
with external data starts only when every dataset it uses has been verified (next section).

## The manifest

An external dataset carries its manifest at a fixed path inside it. The manifest lists every file with its size
and hash:

```json
{
  "format": "market-demo-dataset",
  "version": 1,
  "created_at": "2026-09-28T15:29:24Z",
  "dataset_fingerprint": "<sha256 hex>",
  "file_count": 2206,
  "total_bytes": 1812697046,
  "files": [ { "path": "market/stocks_1min/AAPL_full_1min_adjsplit.parquet", "bytes": 3865509, "sha256": "<hex>" } ]
}
```

- `files` is sorted by `path`, and the paths are relative POSIX paths. The manifest does not list itself.
- `dataset_fingerprint = sha256(json.dumps(files, sort_keys=True, separators=(",", ":")))`. This is the value
  a pack's `pack.yaml` pins.
- Other keys, such as `format`, `proof_scale` or per-symbol row counts, are informational and ignored.
- The pack commits only the fingerprint. The manifest travels with the data: it lists every file name and
  grows with the dataset, so committing it would not scale.

Verification has two levels:

1. **The manifest.** Recompute the fingerprint from `files` and compare it with the pin. A mismatch means a
   different dataset, and nothing proceeds.
2. **The files.** Check each file's size and SHA-256. The results are recorded in
   `$DATA_SOURCE_DIR/<id>/.verified.json` as `path → [bytes, mtime_ns, sha256]`. `fetch` re-hashes only files
   whose size or mtime changed, so it can resume. `prepare` sees the directory read-only: it compares sizes
   and mtimes with the record, which is cheap, and if anything changed it stops and asks for
   `data fetch --verify-only`. Files that the manifest does not list are reported but never deleted.

## `demo.sh data fetch`

`synthetic-market` has no external dataset, so `data fetch` has nothing to do for it. For a pack that
declares one:

```bash
./scripts/demo.sh data fetch                 # every external dataset of $DATA_PACK
./scripts/demo.sh data fetch minute-bars     # one dataset
./scripts/demo.sh data fetch --verify-only   # hash what is already in DATA_SOURCE_DIR
```

The source of dataset `<id>` is the `.env` variable `DATA_SOURCE_<ID>` (upper case, `-` becomes `_`). If
it is empty, fetch only verifies what is already in place.

| Source | Example | Credentials (`.env`) | Runs |
|---|---|---|---|
| Local directory | `/mnt/datasets/minute-bars` or `file:///mnt/…` | – | rsync on the host, then `--verify-only` in the image |
| rsync over SSH | `user@host:/srv/minute-bars` | the host's SSH agent and keys | rsync on the host, then `--verify-only` in the image |
| HTTPS | `https://example.com/minute-bars/` (a prefix; files are fetched as `<prefix><path>`), or one `.tar` or `.tar.gz` archive with the dataset at its top level (a pre-signed URL works) | `DATA_SOURCE_HTTP_TOKEN` (optional, sent as a bearer token) | in the data image |
| S3 or S3-compatible | `s3://bucket/prefix` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_ENDPOINT_URL` | in the data image |
| Google Cloud Storage | `gs://bucket/prefix` | `GOOGLE_APPLICATION_CREDENTIALS`: a host path to a service-account JSON, mounted read-only (empty for a public bucket) | in the data image |
| Hugging Face | `hf://datasets/<owner>/<name>@<revision>/<prefix>` | `HF_TOKEN` | in the data image |

- The procedure (`data/src/demo_data/fetch.py`): get the manifest and check its fingerprint (for an
  archive, after unpacking it into a staging directory). Then fetch each missing or changed file to a hidden
  `.<name>.part`, hashing it while it streams, and rename it only when the hash matches. Downloads run in
  parallel (`DATA_FETCH_JOBS`, default 8) with three attempts each.
- Every URL is an fsspec URL, so one loop serves every scheme; the `data-fetch` one-shot (profile `tools`)
  runs it with `DATA_SOURCE_DIR` writable, as the host user. The remote backends (`s3fs`, `gcsfs`,
  `huggingface_hub`, and `aiohttp` for HTTPS) are the data project's `remote` extra, which the image
  installs; they add about 190 MB to it (508 to 699 MB on x86_64). Local directories and rsync sources are copied on the host,
  because only the host sees them and holds the SSH identity. After every fetch or verification, `demo.sh`
  makes the dataset readable to the services (`chmod -R a+rX`), which run as their own users: a push with
  `rsync -a` keeps the source's modes, and market analytics could not read a directory that arrived as `700`.
- **Credentials.** They live only in `.env`, which is git-ignored. `demo.sh` passes only the variables of
  the chosen scheme to the one-shot fetch container, as `-e NAME` so the values never appear on a command
  line. No long-running service receives them.
- NGC is not a scheme. Download an NGC resource with its CLI, then use it as a local directory.
- `DATA_SOURCE_DIR` defaults to `$HOME/market-demo-data`, outside the repository, so the data cannot be
  staged by accident. `.gitignore` also ignores `data/external/`, `.verified.json` and Parquet, Arrow and
  Feather files outside `data/packs/*/text/`.
- `doctor` (and so `up`) stops when a pack's dataset has not been fetched, naming the variable to set, and
  checks that `DATA_SOURCE_DIR` has room for it (its `bytes` plus 10%).
- **Brev and other VMs.** Point `DATA_SOURCE_DIR` at the large disk. At setup, run `data fetch` from a
  bucket, or push from a laptop with `rsync -a --partial --exclude '.*' <dataset>/ <vm>:market-demo-data/minute-bars/`
  (the default `DATA_SOURCE_DIR`, relative to the VM's home) and then run `data fetch --verify-only` on the VM ([operations](operations.md)).
- Measured on a real minute-bar dataset (2,206 files, 1.81 GB) on a 14-core laptop: `demo.sh data fetch`
  from a local directory (rsync on the host, then hashing every file in the image on a 4-CPU colima VM) took
  15 s; a rerun of `--verify-only` hashed nothing and took 0.1 s. On an A100 VM, `--verify-only` hashed every
  file in 1.3 s, and the whole build took 9.7 s with the SEC snapshot cached, with the same build digest as
  on the laptop.

## Storage layout

**Raw bars are read in place, in the layout they arrive in.** A layout is readable when it is Parquet, when
the symbol can be found in a column or in the path, and when the files are ordered by time.

- **One file per symbol** (for example `<SYMBOL>_full_1min_adjsplit.parquet`), sorted by time, with a time
  column and `open`, `high`, `low`, `close` and `volume`. Reads are partitioned by symbol through the file
  list. When every file carries min/max statistics for its time column, a file wholly outside the window is
  skipped; a file with a single row group and any bar in the window is read whole.
- **The canonical layout** is what `synthetic-market` writes, and what a new real dataset should use:

  ```text
  <dataset>/
    manifest.json
    bars/month=YYYY-MM/part-NNN.parquet   symbol, time, open, high, low, close, volume; sorted by (symbol, time),
                                          row groups of about 122,880 rows, parts of at most about 256 MB that
                                          never split one symbol's month
    companies.parquet                     symbol, company_name, sector, industry, sic_code, exchange, profile,
                                          is_synthetic
    news.parquet                          news_id, symbol, published_at (UTC), source_name, headline, summary,
                                          event_type, sentiment_label, is_story
  ```

  The data is partitioned by date: a month directory prunes a date window. Within each file it is clustered
  by symbol: row-group minimum and maximum values prune symbols. One file per symbol per day would create
  too many small files. Because a part holds whole symbol-months, a scan that reduces each batch per symbol and
  session never sees half a session.

**The build keeps v1's table layout:** one `tables/<table>.parquet` per table, sorted by its natural key, with
row groups of 122,880 rows, and every table loaded into DuckDB with its keys, as in v1. The raw minute bars
never enter the DuckDB file; only the rollup and the minute-bar tools read them.

The design proposed `storage: parquet`: DuckDB views over the build's Parquet instead of copies. It is not
built, because the API opens the database with external access disabled (its data viewer runs user SQL), and a
view over `read_parquet` would fail there. Copying is cheap at this scale: 452,837 daily rows are 14.9 MB of
Parquet and 58 MB of DuckDB, most of it the key indexes (20 MB without them; the load takes 0.65 s with them and
0.1 s without). At 27 million rows that is about 3.4 GB and 40 s, so a pack that large should drop the
`daily_prices` constraints from `schema.sql`.

## Import

### The daily rollup

This is one DuckDB statement over the bars, read in place (`data/src/demo_data/market.py`). It streams, and it
spills to `/data/cache/tmp` past `DATA_DUCKDB_MEMORY` (default: DuckDB's own limit, 80% of the memory it sees).
Only files the manifest lists are read: a file that matches the glob but is not in the manifest stops the build.

```sql
SELECT symbol, CAST(time AS DATE) AS trading_date,
       arg_min(open, time) AS open, max(high) AS high, min(low) AS low, arg_max(close, time) AS close,
       sum(volume) AS volume, sum(close * volume) AS dollar_volume, count(*) AS bar_count
FROM bars                                   -- read_parquet(files, filename = true) with the mapping applied
WHERE CAST(time AS TIME) BETWEEN TIME '09:30' AND TIME '16:00'
GROUP BY ALL ORDER BY symbol, trading_date
```

- The session ends at 16:00 inclusive: in real US minute bars the 16:00 bar carries the closing auction (for
  one large stock, 5.65 million shares against 0.58 million at 15:59 and 2,025 at 16:01).
- On early-close days (for example 2025-11-28 and 2025-12-24), after-hours bars up to 16:00 are counted
  as regular. This is a known approximation; there is no holiday calendar.
- Daily bars (`frequency: 1d`) pass through unchanged.
- A `TIMESTAMP WITH TIME ZONE` time column is converted to wall-clock time in `market.bars.timezone` first.
  `intraday_scan`, which reads the bars in place, converts it the same way. On the GPU, cudf cannot filter such a
  column, so month partitions of it are read by pandas: store naive wall-clock times for GPU speed.
- Measured on 117 million real minute bars: the pass keeps 106 million regular-session bars and writes 572,995
  daily rows (2,200 symbols, 305 dates) to a 12 MB file in 0.9 s on a 14-core laptop, and in 3.3 s in the data
  image on a 4-CPU colima VM. At 100 times the size, expect minutes, once.

**The cache is keyed by the manifest digest.** The key is
`sha256(dataset fingerprint ‖ canonical JSON of market.bars ‖ ROLLUP_VERSION)`. The rollup is written to
`/data/cache/rollups/<key16>/daily_bars.parquet`, with a `rollup.json` beside it recording the inputs, the
row and symbol counts, the date range and the duration. Builds share it: changing the questions, the
exclusions or the SEC snapshot starts a new build but reuses the rollup. `demo-data clean` removes rollups
that no build references, and `clean --all` removes every rollup.

### Tables

The importer turns the rollup, the companies and the news into the fixed table set:

| Table | Built from |
|---|---|
| `trading_sessions` | Dates on which at least half the symbols trading at the time (between their first and last bar) have a regular-session bar. This drops holidays, on which only a handful of symbols have bars, and matches the exchange calendar. `close_at` is the exact close in UTC (20:00 or 21:00, with daylight saving time). |
| `assets` | The symbols left after exclusion, plus company metadata (`cik`, `sic_code`, `sector`, `industry`, `exchange`), `first_session`, `last_session`, `sessions`, `median_dollar_volume` and `liquidity_rank` (over the sessions up to the prediction anchor, so the ranked population leaks nothing), and `is_synthetic` |
| `ticker_history` | One row per asset: its current ticker, from its first session. Past ticker changes are not in the data. |
| `daily_prices` | The rollup on sessions, for kept symbols: `price_id`, `open`, `high`, `low`, `close` (rounded to 4 decimals from float32), `adjusted_close` (equal to `close`: the bars are split-adjusted, dividends are not), `volume`, `dollar_volume`, `bar_count`, `total_return_1d` (0 on an asset's first session) |
| `asset_relationships` | Declared peers: up to `peers` (8) most liquid assets with the same four-digit SIC code, with `peer_rank` |
| `company_news` | The dataset's `news.parquet`, for kept symbols (`synthetic-market` has one) |

`asset_id` is the ticker as the data spells it, for example `NVDA` or `BRK.B`. The same table set and keys serve
every pack.

**Exclusions** are applied in order. Each reason is counted in `pack.json` under
`parts.structured.import.dropped`.

1. `warrants`, `units`, `rights`, `preferred`: another security of an issuer whose stock ticker is its prefix.
   For a symbol SEC lists, the issuer is its CIK: AGNC and AGNCL, AUR and AUROW, ALF and ALFUU share one, while
   MU (Micron) and M (Macy's) do not. The suffix then says what it is: ending in `U` a unit, in `R` or `RT` a
   right, containing `W` a warrant, and anything else a preferred share, depositary share or exchange-traded
   note. Two kinds are share classes and stay: a dotted symbol SEC lists (`BRK.B`, SEC's `BRK-B`), and the
   issuer's primary ticker, the first SEC lists for it (`GOOGL`, beside `GOOG`). A symbol SEC does not list
   goes by the data alone: another symbol plus `W`, `WS`, `U`, `R` or `RT`.
2. `preferred`: any other symbol with a dot that SEC does not list (`ABR.D`).
3. `not_listed`: the symbol is not in SEC's ticker list (ETFs, funds, delisted issuers).
4. `min_sessions`: it has fewer than `min_sessions` daily bars on sessions.

The first design matched suffixes only, which dropped real companies (MU as a unit of M, FR as a right of F)
and kept notes and preferreds with five-letter tickers (AGNCL, BHFAN). Nasdaq share classes still cannot be told
from notes by their symbol (CENTA and CMSA look alike), so a class share that is not its issuer's primary
ticker is dropped as preferred.

On the 2,200-symbol real dataset, these rules kept 1,601 symbols and 452,837 daily rows.

**SEC company metadata** (`companies: sec`, `data/src/demo_data/sec.py`) comes from
`company_tickers_exchange.json` (CIK, name, ticker, exchange; 10,431 tickers) and each kept CIK's
`submissions/CIK##########.json` (`sic`, `sicDescription`). SEC allows 10 requests a second: 1,574 CIKs took
230 s the first time. Both need `SEC_USER_AGENT`. Sector is the SIC division (for example 20–39 Manufacturing,
60–67 Finance, and Nonclassifiable when SEC has no code); industry is the SIC description in title case. A
snapshot is cached in `/data/cache/sec/<date>/`: the ticker file as fetched, and `sic.json` with every CIK
looked up so far, saved as it goes so an interrupted lookup resumes. `prepare` reuses the newest snapshot unless
run with `--refresh-sec`, and the ticker file's digest is part of the build key. This is company metadata only:
SEC filings stay a separate document source.

Measured end to end: with the rollup and the SEC snapshot cached, a new build of that pack (tables, DuckDB,
ontology, prediction) takes 1.1 s on the laptop and 5.1 s in the data image on colima; an unchanged pack is a
no-op in 0.2 s. A build is 72 MB.

## Document sources

| Source | Format | Notes |
|---|---|---|
| `sec_filings` | `edgar-filings` | `synthetic-market` pins a 2026 Q2 sample (1,000 8-K and 6-K filings by any filer, 1,696 documents). A pack of real issuers can pin its own filings manifest; each filing can carry its `ticker` and `items` as citation metadata. |
| `market_regulations` | `ecfr-xml`, `federal-register-xml` | eCFR Title 17 as of 2026-08-17 (3,525 sections), and the SEC's 2023 cybersecurity disclosure rule (88 FR 51896, the Federal Register's XML pinned by SHA-256): its summary and its four appendices of amended form text, five documents. The CFR does not hold the SEC forms' text, so Form 8-K's Item 1.05 and its four-business-day deadline (General Instruction B.1) come from the rule's Appendix C. |
| `world_news` (optional) | `gdelt-parquet`, `opt_in: true` | For a pack with an external dataset that holds GDELT headlines: read in place (`files: gdelt/*.parquet`), only from files its manifest lists and `fetch` verified. One document per headline: `document_id` is `gdelt:<id>`; `title` and `text` are the headline; `url` only when it is https; `published_at` comes from `date` (`YYYYMMDDHHMMSS`, UTC); `metadata` holds `source_domain`, `tone` and `topic` (GDELT's cluster label). A precomputed `embedding` column is never read, so the retriever's model embeds every document. Blank headlines are skipped. |

In `synthetic-market`, the issuers are fictional and the filers are real, and questions keep that boundary
explicit. In a pack of real issuers, filings and prices describe the same companies, and the agent connects them
through each passage's `ticker`: a question such as "February's biggest movers and their 8-Ks" takes a market
tool and a retrieval call, each cited on its own.

**Indexing is batched and resumable.** `retrieval-index` streams `documents.jsonl` (one pass validates it,
a second chunks it) and writes to a build collection named after the corpus digest, in batches of 50 chunks.
If that collection already exists, a run was interrupted: each batch looks up its chunk ids, and only the
missing chunks are embedded. The alias moves to the collection only when it is complete.

## Tool contract and GPU reads

**The contract changes in two places,** and stays `market-analytics/v1`, because no column changes: the news
table becomes optional, and `pack.json`'s `market.bars` feeds `intraday_scan`.

- `analytics.news_table` may be `null`. `$news_table` is then not required, and `validate` does not look
  for it.
- With no news table, the worker holds an empty news frame with the contract's columns, and skips the news
  calls in its warm-up. `sentiment_timeline` and `analyze_news_price_relationship` stay registered, so the
  tool registry, the OpenShell policy and the UI are unchanged. They return `status: "failed"` with
  `error.code: "news_unavailable"` and the message "The active data pack has no ticker-linked news table,
  so this tool is unavailable. Use retrieve_evidence for filings and other documents." Their MCP
  descriptions start with "Unavailable in the active data pack", and the market skill says not to call them.
- `intraday_scan` works the same way with `minute_bars_unavailable`, when `pack.json` has no `market.bars` or
  its `frequency` is not `1min` (`synthetic-market`'s daily-bar profiles report `1d`).
- The receipt's error code is an open identifier, so the API and the generated contracts do not change.
- `session_close_utc` stays a fixed UTC time. A real US pack that sets 21:00, the eastern-standard-time close,
  shows bar timestamps an hour late while daylight saving time is in effect. Without a news table, that shifts
  only the displayed timestamps. A pack that pairs real news with real prices will need a per-row `close_at`;
  that is a contract change, not made now.

**GPU reads come in two tiers.**

| Data | How it is read | Where the memory goes |
|---|---|---|
| Daily tables (the six daily market tools) | Loaded once into the worker at startup, as today: only the needed columns, and timestamps normalized to naive UTC. | Measured: about 135 bytes per daily price row on the GPU and 225 on the CPU, prices and anomaly features together, and a few times that at peak while deriving them. 10,000 symbols over 10 years (about 27 million rows) needs about 3.6 GB of an A100's 40 GB. The GPU service sets `CUDF_PANDAS_RMM_MODE=managed_pool` (cudf.pandas' default where the GPU supports managed memory), so a larger pack pages to host memory instead of failing. At startup the worker logs the estimate from the Parquet metadata. In `sparse_declared_peers` mode the correlation graph is computed from the declared pairs only, so it grows with the pairs, not the square of the symbols. |
| Minute bars (`intraday_scan`; never the six daily tools) | Partition-scoped per request, by `tools/market-analytics/src/market_analytics/bars.py`. The symbols map to files (one per symbol) or to month directories (canonical), and files whose footers show no row group in the window are skipped. The files are read in batches whose estimated uncompressed size stays under `MARKET_ANALYTICS_BATCH_BYTES` (by default 1 GiB on the GPU and 256 MiB on the CPU), one multi-file read per batch (cudf pays about 25 ms per call). In the canonical layout the window and symbols become Parquet row-group filters; one-symbol files are read whole, each row's symbol following from its file's row count. Each batch is reduced to its per-symbol, per-session result before the next is read. `pack.json` carries `market.bars` with `root`, the dataset's directory as the services see it. | One batch at a time, whatever the dataset's size: at peak about 5 to 7 times the batch estimate on the GPU, and 8 to 14 times on the CPU (a whole-market scan of the `synthetic-market` `intraday` profile peaked at 7.1 GiB with one 1 GiB batch and at 2.1 GiB with 256 MiB batches, in less time). Measured on an A100 over the real 1.8 GB dataset: all of its minute bars to session bars in 2.9 s (CPU 46.5 s), and 12 symlinked copies of it (45 GB, read from the page cache) in 30 s (CPU 522 s), peaking at 7.1 GB. Requests are bounded (for example 50 symbols × 30 sessions: 0.2 s). `intraday_scan`: the 50 most liquid over 9 sessions in 0.29 s (CPU 1.2 s), the 500 most liquid over 48 sessions in 1.0 s (CPU 12.3 s), and every stock over 298 sessions, 99 million bars, in 7.5 s (CPU 73 s). |

The rollup, which builds the daily tables, runs in DuckDB on the CPU, streams, and is cached. There is no GPU
rollup: 1.5 s on 117 million minute bars does not justify one. The tools stay on pandas code run by
cudf.pandas, and Polars is not added.

At about 500,000 daily rows, GPU margins are smaller than the 1.5 to 8.1 times measured at 1.36 million rows.
`synthetic-market`'s `standard` profile keeps that scale, and the minute tier is where real data shows GPU work:
`intraday_scan` runs 4 to 12 times faster on an A100 than on its 12 vCPUs, once a request covers more than a
handful of stocks ([measurements](../tools/market-analytics/README.md)).

## The Data Designer pack

### Verified on 2026-09-29

- `data-designer` 0.9.3 (released 2026-09-21) is the current release. It installs `data-designer-config`
  and `data-designer-engine` 0.9.3 and needs Python 3.10 or later.
- The API: `DataDesigner(model_providers=[...]).create(builder, num_records=, resume=ResumeMode.IF_POSSIBLE)`,
  `DataDesignerConfigBuilder(model_configs=[...])`, `with_seed_dataset(DataFrameSeedSource(df=...),
  sampling_strategy=SamplingStrategy.ORDERED)`, `LLMStructuredColumnConfig(output_format=<Pydantic model>)`,
  `LLMTextColumnConfig`, `ExpressionColumnConfig` and `CustomColumnConfig`.
- **Sampler columns cannot be seeded.** The engine builds each sampler's generator with `random_state=None`,
  and no configuration reaches it. Seeded numbers therefore come from our code, not from Data Designer's
  samplers (below).
- **`api_key` is an environment variable name**, resolved when a request is made. The built-in `nvidia`
  provider reads `NVIDIA_API_KEY`, which this repository never uses. So the pack defines its own provider:
  `ModelProvider(name="build", endpoint=DATA_DESIGNER_BASE_URL, provider_type="openai",
  api_key="DATA_DESIGNER_API_KEY")`.
- **Dependencies conflict with the data project:** 0.9.3 needs `pyarrow>=24,<25`, and `demo-data` pins
  `pyarrow==25.0.1`. Generation therefore gets its own uv project, run on the host (below).
- **Telemetry is on by default.** `NEMO_TELEMETRY_ENABLED` defaults to `true`: each batch posts an event with
  the model id and token counts to NVIDIA's telemetry endpoint, and each model request carries an `X-Title`
  attribution header. `demo-data-generate` sets it to `false` before importing Data Designer, unless you set
  it yourself, as the rest of the stack turns off OpenShell's and Phoenix's telemetry.
- **A live preview** on build.nvidia.com with the nvapi key worked, once the model timeout was raised to
  120 s (the default health check timed out):
  - Plain text on `nvidia/nemotron-3.5-lightning-30b-a3b` produced one degenerate name out of three.
  - A structured column on `nvidia/nemotron-3-super-120b-a12b` with thinking off gave clean names and
    profiles.
  - In both, names repeated across rows: 4 of 6 structured rows were duplicates.
  - The design below answers each of these.

### Two stages

| | `demo.sh data generate [--profile P]` | `demo.sh data prepare` |
|---|---|---|
| Runs | Rarely: to change the text, or for a profile larger than the committed text | Every build |
| Code | `data/generate/`: its own uv project, with `data-designer==0.9.3`, run with uv on the host | `data/packs/synthetic-market/generator/build.py` in the data image (numpy and pyarrow) |
| Needs | `DATA_DESIGNER_API_KEY` (defaults to `INFERENCE_API_KEY` for the same endpoint, or an nvapi- key on build.nvidia.com), `SEC_USER_AGENT` | Nothing: no key, no network |
| Writes | The **text**: `companies.jsonl`, `headlines.jsonl`, `stories.jsonl` (one row per line, so a review sees each) and `checks.json` | The raw dataset in the canonical layout, in `/data/cache/generated/<key16>/`, then the normal import |
| Deterministic | No: LLM output. It is reviewed and committed; the pack digest covers it. | Yes: identical bytes for identical seed, parameters, text and code |

The text for `standard` is committed in `data/packs/synthetic-market/text/`, at a few hundred kilobytes, and
reviewed like any other change; `generate` rewrites it there for review, keeping every row that still passes
the checks. A profile needing more issuers than the committed text needs `generate --profile <p>` first, which
extends `text/` in place, and then `demo.sh up`, which rebuilds the data image that carries the pack;
otherwise `prepare` stops and says so. Rosters are prefix-stable, so every smaller profile uses the first N rows
of the text, and the pack digest covers the text, so a build never reuses stale text.

`DATA_DESIGNER_BASE_URL` defaults to `https://integrate.api.nvidia.com/v1`, and `DATA_DESIGNER_MODEL` to
`nvidia/nemotron-3-super-120b-a12b` (thinking off, temperature 0.7, timeout 120 s). `demo.sh` passes the keys
in the environment of the one `uv run` only, and the inference key stands in only for its own endpoint (or, as
an nvapi- key, for build.nvidia.com): a gateway key is never sent to build.nvidia.com.

### The scale knob

The knob is the generator profiles in `pack.yaml`, selected with `DATA_PACK_PROFILE`. To scale further, add
a profile.

| Profile | Issuers | Dates | Bars | Daily rows | Minute rows | Text |
|---|---|---|---|---|---|---|
| `ci` | 12 (the story issuers) | 2026-06-01 to 2026-08-31 | 1min | 768 | 300,288 | committed |
| `interactive` | 50 | 2025-01-02 to 2026-08-31 | 1d | 20,800 | – | committed |
| `standard` (default) | 2,000 | 2024-01-02 to 2026-08-31 | 1d | 1,336,000 | – | committed |
| `intraday` | 500 | 2026-03-02 to 2026-08-31 | 1min | 63,500 | 24,828,500 | committed |
| `large` | 10,000 | 2016-01-04 to 2026-08-31 | 1d | 26,800,000 | – | `data generate` |

The pack declares `frequency: 1min` for every profile: a daily-bar profile writes one bar per session, stamped
at the 16:00 close, which the regular-session rollup keeps unchanged (`bar_count` 1).

The committed text lives in `text/`, so the `.parquet` rule in `.gitignore` allows `data/packs/*/text/`.

### Seeded numbers

Every random draw comes from `np.random.default_rng([seed, crc32(stream), *keys])`, with one named stream
per concern (`roster` and `name-root` in `generate`; `market`, `industry`, `issuer`, `issuer-noise`, `news`
and `minutes` in `prepare`), keyed by issuer slot. A profile's first N issuers are therefore identical in every
profile, and adding issuers changes no existing series. The market is simulated over the whole 2016 to 2026
calendar and each profile keeps its window, so an issuer also has the same prices in every profile.

- **Roster** (the seed dataset handed to Data Designer): `slot`, a pronounceable invented `name_root`, a
  four-letter `ticker`, a real `sic_code`, its SEC `industry` and SIC-division `sector`, `exchange`, and
  `is_story` for slots 0 to 11. The price-model parameters are drawn at `prepare` from the slot's stream.
- **Returns**: `r[i,t] = β_i·market_t + γ_i·industry_{k,t} + drift_i + σ_i·ε[i,t] + news[i,t]`. Each
  factor and ε is Student-t with 4 degrees of freedom scaled to unit variance, over a calendar with the NYSE
  holidays for 2016 to 2026 (computed) and two unscheduled closures. Issuers in one SIC industry share its
  factor, so same-SIC declared peers are correlated. Opens gap from the previous close; highs and lows come
  from a half-normal range.
- **Volume**: a daily dollar amount per issuer, rising with |r| over the issuer's total volatility, and 2 to 4
  times higher on news sessions. The story issuers' dollar volume is far above every other issuer's, so they
  are always the 12 most liquid: the `top_12` universe and the prediction population, with no extra column.
- **News**: background items arrive per issuer as a Poisson process (1 a month; 3 for story issuers), with a
  type and a sentiment label. The next session's return tilts ±0.3σ with the label: a modest, planted
  relationship that `analyze_news_price_relationship` finds. There are 12 story items, one per story issuer,
  published from 2026-08-17 to 2026-08-28, each with a fixed shock of ±6 to 12% on its publication session and
  30% of that again over the next five sessions.
- **Minute bars** (1min profiles): 391 bars per session, 09:30 through the 16:00 closing auction. Each session
  is a Brownian bridge from the day's open to its close, clipped to its range, with U-shaped volume; the bars
  at its highest and lowest points take the day's high and low, so the rollup reproduces the daily bar exactly.
- Model constants live in `generator/model.yaml`, not in code.

### Nemotron text

The roster is Data Designer's seed dataset, read in order (`SamplingStrategy.ORDERED`), so each LLM row is
tied to one seeded slot. The seed is written to a Parquet file named after its content and passed as a
`LocalFileSeedSource`: Data Designer's resume fingerprint covers a seed file's path but leaves a
`DataFrameSeedSource`'s rows out, so resuming with a DataFrame seed could return another seed's answers.

| Data Designer job | Records | Column (structured, Pydantic `output_format`) |
|---|---|---|
| `companies` | one per issuer | `company_name` (at most 40 characters, built on `name_root`, no legal suffix) and `profile` (one sentence, at most 240 characters) |
| `headlines` | event type × sentiment (30) | `templates`: 12 per record, each at most 110 characters with exactly one `{company}` placeholder |
| `stories` | the 12 story events (issuer, date, type, sentiment) | `headline` (at most 110 characters) and `summary` (at most 600 characters) |

A background news row's headline is a template drawn by its seeded stream and filled with the company name.
The number of LLM calls therefore grows with the issuers, not with the news volume. build.nvidia.com's rate
limit is what bounds `generate`, and `resume=IF_POSSIBLE` lets an interrupted run continue. Rows already in
the output are kept while they pass the checks, so a rerun asks only for what is missing or failed.

### Checks, at generate time

A row that fails is asked again, for at most 5 rounds; after that, `generate` stops.

- **Shape.** The Pydantic formats enforce the lengths. A template must have exactly one placeholder.
- **Unique.** Normalized names, tickers and roots must be unique within the pack. Normalizing means
  casefolding, stripping punctuation and dropping legal suffixes: inc, corp, corporation, co, company, ltd,
  plc, holdings, group, llc, lp, sa, nv, ag, trust, the.
- **No real collisions**, checked against SEC's `company_tickers.json`, `company_tickers_exchange.json`
  and `company_tickers_mf.json`, fetched with `SEC_USER_AGENT`:
  - no ticker may be an SEC ticker;
  - no normalized name may equal a normalized SEC title;
  - no `name_root` may appear as a word in any SEC title.
  A slot that fails draws its next root from its own stream, and that slot alone is regenerated, for at most
  5 rounds. So the output is deterministic for a given SEC snapshot.
- **Fit to show.** Random syllables can spell profanity or slurs, in a root or in a ticker spelled from it.
  A root or ticker containing a fragment from `roster.UNFIT` is skipped like a taken one.
- **Labels.** `assets.is_synthetic` is true. News `source_name` is "Synthetic Newswire". The source is
  `synthetic: true`, so the UI shows its badge. The pack disclaimer says that the issuers, prices and news are
  fictional.
- **Format.** A name must start with its root, have at most three more words and no legal suffix; a story
  headline must name its company.
- **Record.** `checks.json` records the SEC files' URLs, SHA-256 values and fetch time, the counts, the
  rejects per round, the model id, the Data Designer version, the seed, the calls and tokens used, and each
  text file's SHA-256.
- The contract tables are validated again at `prepare`, like every pack.

## Questions

`questions.yaml` lists the pack's questions. Six are featured, and all six need only the default profiles
(`core,retrieval,analytics`), so the landing page stays on one screen. Kumo (PREDICTION) and Auto Ontology (SQL)
questions are listed but not featured. The wording was checked against local builds, and every filing an answer
should cite is in the pack's manifest. The live runs come with the recordings.

### `synthetic-market` (as of 2026-08-31; the 12 most liquid issuers are the story issuers)

| Id | Tag | Sources | Question | Tools |
|---|---|---|---|---|
| `market-leaders` ★ | ANALYTICS | `market_data` | Among the 12 most liquid issuers, which had the strongest and weakest returns over the 20 trading sessions ending August 31, 2026, and how did their daily volatility compare? | `market_scan` |
| `news-sentiment-reaction` ★ | ANALYTICS | `market_data` | For company news about the 12 most liquid issuers published August 17–24, 2026, how did the sentiment labels line up with the following five sessions' returns? Without claiming causation. | `sentiment_timeline`, `analyze_news_price_relationship` |
| `unusual-sessions` ★ | ANOMALY | `market_data` | With January 2 to June 30, 2026 as the baseline for every issuer, which 10 issuer sessions from July 1 to August 31 were most unusual in return, volatility and volume, and why? | `market_anomaly_scan` |
| `peer-network` | GRAPH | `market_data` | In the return-correlation network from June through August 2026, which issuers are most central, and which pairs moved together most closely? | `analyze_market_relationships` |
| `cyber-disclosure-rules` ★ | RETRIEVAL | `sec_filings`, `market_regulations` | What does Form 8-K Item 1.05 require after a material cybersecurity incident, and by when? Cite the regulation and any 2026 Q2 filings that report an incident. | `retrieve_evidence` (expected citations: 17 CFR 229.106 and 249.308, the rule's Form 8-K appendix; one filing, CB Financial Services) |
| `news-and-filings` ★ | HYBRID | `market_data`, `sec_filings` | Which of the 12 most liquid issuers had the most negative company news in July and August 2026, and how did their prices react? Separately, which real 2026 Q2 filings describe operational disruptions? Keep them apart. | `sentiment_timeline`, `price_context`, `retrieve_evidence` (expected filings: TotalEnergies, B2Gold, Centerra Gold, Cactus) |
| `story-event-context` | ANALYTICS | `market_data` | Each story's return on its publication session and the two after it | `price_context` |
| `outcome-prediction` | PREDICTION | `market_data` | At the August 24, 2026 anchor, the 12 most liquid issuers by likelihood of a positive five-session return | `predict_asset_outcomes` |
| `sector-sql` | SQL | `market_data` | Issuers and median 20-session return by sector | `ask_question` |
| `large-universe-scan` | ANALYTICS | `market_data` | The 20 strongest and weakest of every issuer, January 2024 to August 2026 (`standard`, `large`) | `market_scan` |
| `intraday-ranges` | INTRADAY | `market_data` | The story issuers' widest intraday swings, August 17–28, 2026, with open-to-close moves and last-30-minute volume (`ci`, `intraday`) | `intraday_scan` |

## Disk

For a pack with a 1.8 GB external minute-bar dataset:

| Item | Size | Scales with |
|---|---|---|
| `DATA_SOURCE_DIR/<dataset>` | 1.8 GB | the dataset |
| Rollup cache | 12 MB | about 1% of the minute data |
| SEC snapshot | 0.6 MB | the issuers |
| A build (tables 15 MB, DuckDB 58 MB, ontology) | 72 MB | the daily rows |
| The build's corpus text (`sec_filings`, `market_regulations`) | 47 MB | the corpus selection |
| Auto Ontology's database for the pack (`ontology` profile; one per pack) | 72 MB | the tables and columns |
| SEC and eCFR downloads, the Milvus index | about 2 GB | the corpus selection |
| The data image, with the `remote` fetch backends | 699 MB (x86_64) | – |

`doctor` compares the free space on `DATA_SOURCE_DIR` with the pack's `external.<id>.bytes`, plus 10%, for
every dataset not fetched yet. It asks for `SEC_USER_AGENT` when the pack looks up its companies at SEC or builds
the SEC filings corpus. On a VM, the images and the Docker volumes stay where Docker keeps them
([operations](operations.md)), and only `DATA_SOURCE_DIR` moves to the large disk.

## Where it lives

| Area | What |
|---|---|
| `data/schemas/pack.schema.json` | v2: `external`, `market`, provenance kind `external`, in-place corpora (`files`), nullable `news_table`, `population` by view |
| `data/src/demo_data/` | `external.py` (manifest, fingerprint, verification), `fetch.py` (sources), `market.py` (rollup, import), `sec.py` (company metadata), `corpus/` (`edgar.py`, `ecfr.py`, `gdelt.py`, `markdown.py`), `structured.py`, `cli.py` |
| `data/generate/` | The Data Designer jobs and their checks |
| `data/packs/` | `synthetic-market/`, with its `recordings/` |
| `tools/market-analytics/` | The contract (optional news table, minute bars); `bars.py` (minute-bar scans) and `tools/intraday.py` (`intraday_scan`); the availability of each tool per pack; the sparse peer graph; the memory estimate |
| `tools/retrieval/` | Streamed, resumable indexing |
| `scripts/demo.sh`, `scripts/lib/doctor.sh`, `compose.yaml`, `.env.example` | `data fetch` and `data generate`, the `/sources` mounts, the `DATA_SOURCE_*`, `DATA_DESIGNER_*` and credential variables, the default `DATA_PACK` and `DATA_DATABASE_NAME` |
| `.gitignore` | `data/external/` and the committed-data rules |
