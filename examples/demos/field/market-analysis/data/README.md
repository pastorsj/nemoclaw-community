# data

Data packs, and `demo-data`, the builder that turns the selected pack into files the services read.

A **pack** is one directory under `packs/` that describes a whole demo world: its tables, its document corpora, the
source catalog the UI and agent see, analytics and prediction settings, and the demo questions. `demo-data` builds
the pack named by `DATA_PACK` into `/data` and points `/data/active` at the result. Services never read `packs/`;
they read only `/data/active`, so swapping data means adding a pack, not editing code.

The repository ships one pack, [`synthetic-market`](packs/synthetic-market/README.md), the default: a fictional
market whose names and news text were written with NeMo Data Designer and Nemotron
([`generate`](generate/README.md)); it builds with no key and no download. A pack of your own can hold real prices
from an **external dataset**: data that lives outside the repository, is never committed, and reaches a machine
through `demo-data fetch` ([data platform](../docs/data-platform.md)). In every pack, SEC EDGAR filings are a
separate document source for retrieval, never turned into news. A pack's `recordings/` holds its replay bundle.

## How it fits

| Who | When | Reads | Writes |
|---|---|---|---|
| `data-fetch` one-shot (`demo.sh data fetch`) | when a pack's external data is new | a source (URL) | `/sources/<dataset>/` (the host's `DATA_SOURCE_DIR`) |
| `data` one-shot (profile `core`) | before the API starts | `packs/$DATA_PACK`, `/sources` (read-only) | the structured part, `pack.json`, `/data/active` |
| `data-corpus` one-shot (profile `retrieval`) | after `data` | `packs/$DATA_PACK`, pinned public files, `/sources` (in-place corpora) | `corpus/documents.jsonl`, `pack.json` |
| `retrieval-index` one-shot | after `data-corpus` | `corpus/documents.jsonl` | `collection-manifest.json` |
| `api` | runtime | `pack.json` (sources, questions, disclaimer), the DuckDB file (read-only) | – |
| `market-analytics` | runtime | `pack.json` (`analytics`, `prediction`, `market`), `tables/*.parquet`, the DuckDB file, the raw minute bars (`/sources`, or a generated dataset in `/data/cache`) | – |
| Auto Ontology | runtime | the DuckDB file, `ontology/model.yaml` | – |

All of them share the `demo-data` volume, mounted at `/data`.

## Runtime layout

```
/data/
  active -> builds/<pack>@<version>+<profile>+<digest12>    swapped atomically; readers use only this path
  builds/<build>/
    pack.json                          the pack as this build serves it (below)
    tables/<table>.parquet             every table, including ones kept out of DuckDB
    structured/<database_name>.duckdb  schema.sql + tables + rendered views (e.g. prediction.*)
    ontology/model.yaml                Auto Ontology model, derived from the database and ontology.yaml
    prediction/graph.json              prediction views, keys, time columns, anchor, population, row counts
    prediction/templates.json          PQL templates
    corpus/documents.jsonl             one whole document per line (schemas/documents.schema.json)
    collection-manifest.json           written by retrieval-index, not by demo-data
  downloads/sha256/<digest>            pinned public files, fetched once and shared by every build
  cache/rollups/<key>/                 daily rollups of minute bars, keyed by dataset fingerprint and bar settings
  cache/generated/<key>/               raw datasets a pack's generator wrote, for the same import
  cache/sec/<date>/                    SEC company snapshots: the ticker list and the SIC codes looked up
/sources/<dataset>/                    external datasets (the host's DATA_SOURCE_DIR), read-only except to fetch
```

A build's name ends in a digest of everything it depends on: the pack's files (not `README.md` or
`recordings/`), the profile, the selected corpora, the builder itself and, for a pack with SEC company
data, the SEC snapshot. An external dataset needs nothing extra: `pack.yaml` pins its fingerprint. Preparing an
unchanged pack is a no-op; changing any input starts a new build, and the old one stays until `demo-data clean`.

`pack.json` holds the resolved pack: `id`, `version`, `title`, `disclaimer`, `profile`, `licenses`, `provenance`,
the `sources` and `questions` this build can serve (a question needs all its sources and, if it names profiles, one
of them), `structured` (`database_name`, the database path, each table's Parquet path), `analytics` (universes and
graph mode resolved for the profile), `prediction` (with the population's ids), `market` (the bar settings and the
raw dataset's `root`, for tools that read minute bars), `documents` (collection, sources, path) and `parts`: the row
and document counts, the import's receipt and the SHA-256 of every file each part wrote. A build becomes active once
its structured part is recorded; the corpus part can be added to it later. Until then, `pack.json` has no
`documents` and lists neither the document sources nor the questions that need them.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATA_PACK` | `synthetic-market` | the pack to build, a directory under `packs/` |
| `DATA_PACK_PROFILE` | the pack's `default_profile` (`standard`) | generator profile |
| `DATA_CORPORA` | every corpus not marked `opt_in` | comma-separated corpus sources, e.g. `market_regulations` |
| `SEC_USER_AGENT` | – | required to fetch SEC EDGAR filings and SEC company data: a name and an email, e.g. `Example Co admin@example.com` |
| `DATA_DIR` | `/data` | where builds live |
| `DATA_SOURCE_DIR` | `/sources` | where external datasets live, one directory per dataset (on the host: `$HOME/market-demo-data`) |
| `DATA_SOURCE_<ID>` | – | where `fetch` gets dataset `<id>` (upper case, `-` as `_`): a path or an fsspec URL, below |
| `DATA_FETCH_JOBS` | `8` | files `fetch` downloads and hashes in parallel |
| `DATA_DUCKDB_MEMORY` | DuckDB's default | a memory cap for the rollup and import, e.g. `8GB`; past it they spill to `/data/cache/tmp` |
| `DATA_PACKS_DIR` | `data/packs` | where packs live |
| `DATA_CONTRACTS_DIR` | `tools/*/contract/` | the tool contracts to check packs against |

`DATA_PACK_PROFILE` and `DATA_CORPORA` are part of the build key, so they must be identical for the `data` and
`data-corpus` one-shots. Otherwise `prepare --corpus` lands in a build with no structured part, and exits 1.

The database name is the pack id in snake case (`synthetic-market` → `synthetic_market`), and `validate` enforces it.
Services that read `/data/active/pack.json` should take it from `structured.database_name`. For the others,
`scripts/demo.sh` (bash) exports `DATA_DATABASE_NAME="${DATA_PACK//-/_}"`, and `compose.yaml` uses
`${DATA_DATABASE_NAME:-synthetic_market}`, since Compose interpolation has no pattern substitution. A raw
`docker compose up` without `demo.sh` therefore gets the default pack's `synthetic_market`, whatever `DATA_PACK` says,
unless `DATA_DATABASE_NAME` is set as well.

## Commands

```bash
demo-data validate                # schema, cross-references, and the tool contracts the pack declares
demo-data fetch [ID...]           # the pack's external datasets from DATA_SOURCE_<ID>, verified file by file
demo-data fetch --verify-only     # hash what is already in DATA_SOURCE_DIR (only files that changed)
demo-data prepare                 # build (or reuse) the structured part and the corpus, then activate
demo-data prepare --structured    # tables, DuckDB, ontology, prediction (about 6 s for synthetic-market standard)
demo-data prepare --corpus        # corpus/documents.jsonl (the first EDGAR run downloads about 1.3 GB)
demo-data verify                  # the active build still matches the digests in its pack.json
demo-data list                    # packs and builds
demo-data clean [--all]           # remove inactive builds and unused caches (--all: every cache and download)
```

## External datasets

A pack pins each external dataset in `pack.yaml` by the **fingerprint** of the manifest the dataset carries:
`sha256` of its `files` list (`path`, `bytes`, `sha256` per file) as compact JSON with sorted keys. The
repository never holds the data or even its manifest, only that pin.

`fetch` reads the manifest from the source first and stops if the fingerprint differs. It then streams each
missing or changed file to a hidden `.part` file while hashing it, and renames it into place only if the hash
matches; a file that still fails after three attempts does not stop the others. `<dataset>/.verified.json`
records every verified file's size, mtime and hash, so a rerun hashes only what changed and an interrupted or
failed fetch resumes. An https source is read with one plain GET per file, since some servers refuse Range
requests. `prepare` sees the dataset read-only and only compares sizes and mtimes with that record; if anything
changed it stops and asks for `fetch --verify-only`.

Sources are fsspec URLs: a local path or `file://`, `https://host/prefix/` (each file is `<prefix><path>`) or one
`.tar`/`.tar.gz` archive with the dataset at its top level, `s3://bucket/prefix` (`AWS_*`, including
`AWS_ENDPOINT_URL` for S3-compatible stores), `gs://bucket/prefix` (`GOOGLE_APPLICATION_CREDENTIALS`; a public
bucket needs none) and `hf://datasets/<owner>/<name>@<revision>/<prefix>` (`HF_TOKEN`). `DATA_SOURCE_HTTP_TOKEN`
is sent to https sources as a bearer token. The remote backends are the `remote` extra, which the image installs
(`uv sync --extra remote` locally). `scripts/demo.sh data fetch` copies local directories and `host:/path`
sources with rsync on the host instead, then runs `fetch --verify-only`.

## The market importer

A pack with a `market` section gets its tables from `demo_data/market.py`, not from a generator: one streaming
DuckDB pass rolls the raw bars up to daily bars (cached by the dataset's fingerprint and the bar settings), then
SQL on the daily rows derives `trading_sessions`, `assets`, `ticker_history`, `daily_prices`,
`asset_relationships` and, if the dataset has one, the ticker-linked news table. Company data comes from SEC
(`companies: sec`: names, CIKs and exchanges from `company_tickers_exchange.json`, SIC codes from each company's
submissions) or from a table in the dataset. `market.bars.dataset` names either an `external` dataset or the
pack's generator, whose output (a raw dataset with its own manifest) is imported the same way. What the import
kept and dropped is recorded in `pack.json` under `parts.structured.import`. The rules are in
[data platform](../docs/data-platform.md#import).

## The pack format

`pack.yaml` follows [`schemas/pack.schema.json`](schemas/pack.schema.json); its paths are relative to the pack.

- **Identity**: `id`, `version` (minor for data changes, major for table or column changes), `title`, `description`,
  `as_of`, `disclaimer`, `licenses`, and `provenance`: every origin (`generated`, `committed`, `download` or
  `external`) with the environment variables that fetching its documents needs.
- **`external`** (`schema_version: "2"`): datasets outside the repository, each with its manifest's path, pinned
  `fingerprint` and size. **`market`**: how the importer reads the bars (files, symbol, columns, frequency, time
  zone, regular session), where company data and news come from, the exclusions, `min_sessions` and `peers`.
- **`generator`**: a pack-local program, run as `python <entrypoint> --profile <p> --out <dir>`, that writes the raw
  market dataset (bars, companies, news, `manifest.json`) the importer reads, as a real dataset would arrive.
  `profiles` hold its parameters and the row counts each profile must produce; a profile's `frequency` (`1min` or
  `1d`) is the frequency of the bars it writes, which `pack.json` reports.
- **`structured`**: the source it serves, `database_name`, `schema` (DDL with primary and foreign keys), `views`
  (SQL run after loading, with `{{anchor_date}}`, `{{anchor_timestamp}}`, `{{horizon_sessions}}` and
  `{{population_literals}}` filled from `prediction`) and `tables`: the importer's, each with the origin of
  `market.bars.dataset` (`load_into_database: false` keeps a table out of DuckDB). Tables load by column name, so
  Parquet column order does not matter.
- **`documents`**: the collection name and `corpora`, each with a `format`. A pinned corpus (`ecfr-xml`,
  `federal-register-xml`, `edgar-filings`, `markdown`) has a `manifest` in the pack that pins what to build. An
  in-place corpus (`gdelt-parquet`) has `files`, a glob inside its external origin's dataset, read where `fetch` put
  it. A source may draw on several corpora (`market_regulations`: the eCFR and a Federal Register rule), all of
  them `opt_in` or none: an `opt_in` corpus is built only when `DATA_CORPORA` names its source.
- **`sources`**: the catalog: `name` and `description` for the UI, `agent_description` for the agent.
- **`analytics`**, **`prediction`**: settings for the market-analytics and prediction tools. `analytics.news_table`
  is null for a pack without ticker-linked news; the news tools then report that they are unavailable.
  `prediction.population` may name only a `view`; the build writes the ids it selects into `pack.json`.
- **`ontology`**: table and column descriptions. **`questions`**: the demo questions
  ([`schemas/questions.schema.json`](schemas/questions.schema.json)).

Tools own their contracts. A pack that sets `analytics.contract: market-analytics/v1` must provide what
`tools/market-analytics/contract/market-analytics.v1.json` lists. `validate` checks the tables `schema.sql`
declares; `prepare` checks the built tables, including enumerations and unique keys. `demo-data` never imports tool
code.

## Adding or swapping a pack

1. Copy `packs/synthetic-market` to `packs/<new-id>`. For your own bars, replace its `generator` with a dataset
   pinned under `external` and describe their layout under `market.bars`
   ([data platform](../docs/data-platform.md#pack-format-v2)).
2. Edit `pack.yaml` (provenance, licenses, sources, disclaimer, universes, prediction), `schema.sql`,
   `ontology.yaml` and `questions.yaml`. Only the columns in the tool contracts are mandatory.
3. Run `DATA_PACK=<new-id> demo-data validate`, then `prepare`. No code changes are needed while the contracts hold.

## DuckDB versions

An older DuckDB cannot always open a file a newer one wrote, so the writer here must never be newer than the readers
(api, market-analytics, Auto Ontology). `duckdb` is pinned to the readers' version, `structured.py` pins the file's
storage format, and every uv project that reads the file must lock the same or a newer DuckDB. Upgrade the readers
first, then this pin.

## Development

```bash
cd data
uv sync
uv run demo-data --data-dir /tmp/demo-data prepare --profile interactive --corpora market_regulations
docker build -f Dockerfile -t market-demo/demo-data:dev ..   # the build context is this example's root
```
