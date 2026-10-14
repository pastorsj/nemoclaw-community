# market-analytics

An MCP server (streamable HTTP at `:3010/mcp`) with seven read-only market tools, plus Kumo prediction when a
Kumo Relational service's URL and key are configured. Hermes calls them as `mcp__market_analytics__<tool>`. The tools run on
pandas, scikit-learn and NetworkX; the GPU image runs the same code on RAPIDS (cuDF, cuML, cuGraph).

| Question | Tool | CPU | GPU |
| --- | --- | --- | --- |
| Leaders and laggards by return, volume, volatility or peer-relative return | `market_scan` | pandas | cudf.pandas |
| Sessions that behave unusually against an earlier baseline | `market_anomaly_scan` | scikit-learn PCA | cuML PCA, on the device |
| Return, price range and volume for named assets | `price_context` | pandas | cudf.pandas |
| Positive, neutral and negative news labels over time | `sentiment_timeline` | pandas | cudf.pandas |
| How news sentiment lined up with later returns | `analyze_news_price_relationship` | pandas | cudf.pandas |
| Most central assets in the return-correlation graph | `analyze_market_relationships` | `nx.pagerank` | nx-cugraph |
| Sessions ranked by intraday range, minute volatility, drawdown or volume timing, from the raw minute bars | `intraday_scan` | pandas | cudf.pandas |
| Probability of a curated future outcome per asset | `predict_asset_outcomes` | Kumo Relational (a NIM on its own GPU host) | same |

The results are descriptive. Each result names the device, library and time it used, and nothing more; the
measured difference between the two engines is under [CPU and GPU timings](#cpu-and-gpu-timings).

## How it fits

- **Data.** The `data` one-shot builds the active pack at `/data/active`. The server reads `pack.json`, the
  Parquet tables under `tables/`, and (for prediction only) `structured/<database_name>.duckdb`. It checks the
  tables against [`contract/market-analytics.v1.json`](contract/market-analytics.v1.json) at startup, the same
  file `demo-data validate` uses. Universes, the graph window and the prediction templates come from the pack,
  so no dataset facts live in this code. The server resolves the `/data/active` symlink once at startup, and
  every worker, including a replacement, loads that build: restart the service after activating another pack.
- **Worker.** One spawned process loads and derives the tables once (prices, anomaly features, news aligned to
  sessions, the correlation graph), runs every tool the pack supports once as a warm-up so the first question does not pay the GPU
  libraries' first-call costs, and runs every call under a deadline. A call that overruns, or a crash,
  kills the worker and a fresh one replaces it, so the next call succeeds. The tools are async and wait for the
  worker on a thread, so `tools/list` and other requests stay responsive. `GET /health` is 200 while the
  worker is up.
- **Results.** Every market tool returns a `MarketResult` (see `src/market_analytics/models.py`): `status`
  (`succeeded`, `empty` or `failed`), `source_id`, `database_name`, the tool's `payload`, `error`,
  `engine {device, library, version, engine_id}`, `timing {compute_ms, setup_ms, engine_ms, total_ms}`,
  `rows_scanned`, `asset_count`, `warnings` and `limitations`. This is what the execution receipt shows. The
  engine id names the method and device (`cudf-gpu.v1`, `cuml-pca-anomaly-gpu.v1`, `cugraph-pagerank-gpu.v1`
  and their CPU twins `pandas-cpu.v1`, `sklearn-pca-anomaly-cpu.v1`, `networkx-pagerank-cpu.v1`). The timing is
  in milliseconds to the microsecond: the worker's calculation, its other work on the call (the arguments before,
  the result after), the two together, and the call end to end in the service, including the wait for the worker.
  `market_anomaly_scan`'s payload names its policy, `pca-reconstruction-market-v1`. Failures (bad arguments, deadlines, a dead worker)
  are results too, because MCP clients drop the structured content of an error response. An error message is
  at most 1,000 characters, the receipt's limit.
- **Result size.** Hermes hides an MCP result longer than 50,000 characters from the model
  ([tool result size](../../docs/architecture.md#tool-result-size)), so `budget.py` caps each list a result
  holds (50 `market_scan` assets, 25 anomalies, 30 intraday sessions, 100 sentiment periods, 50 news events and
  50 assets' news summaries) and then shortens the least important list while the result is longer than 30,000
  characters as the agent reads it. Its `warnings` summarize the rows left out (ranks and the range of the value
  that ranked them, or label counts), and the payload's `*_truncated` flag is set.
- **Z-scores and news coverage.** `market_scan` reports every requested metric's z-score among the universe's
  assets (`zscores`, population standard deviation), so a metric that does not rank still says how unusual a
  value is. `analyze_news_price_relationship` lists and counts every article in the window: one whose horizon
  runs past the data has a null `forward_return` and `outcome_session` but keeps its `session_return`, and
  `asset_summaries` counts each asset's labels over every article. Its per-label summaries and correlation use
  only the articles with a forward return.
- **Timestamps.** Inside the worker every timestamp is tz-naive UTC `datetime64[ns]`: `data.py` normalizes the
  tables once at load, the dispatcher converts timezone-aware arguments to UTC, and the result models put the
  UTC offset back, so results still read `2026-08-24T21:00:00Z`. cudf.pandas cannot keep a tz-aware column on
  the GPU; with one, every operation on the frame fell back to pandas.
- **Returns.** A return over a window runs from the close before the window's first session to its last close,
  so "the 20 sessions ending D" are 20 daily returns, the same ones the volatility beside it uses. An asset with
  no earlier session starts from its first close. `market_scan` and `price_context` agree.
- **Benchmark.** `POST /benchmark` `{tool, arguments, pairs, budget_seconds}` runs one tool call on both
  engines for the UI's Benchmark tab; the API calls it with the arguments a receipt recorded
  (`api/README.md`). Only the GPU service compares: its GPU worker runs the call, and a CPU worker
  (`MARKET_ANALYTICS_ENGINE=cpu` in that process only), started on the first request and kept, runs the
  same MCP tool on pandas, scikit-learn and NetworkX. Each engine runs the call once untimed, then the two
  run it in pairs, alternating which goes first, until `pairs` pairs or the budget is spent (at least
  one pair). A trial's time is the tool's compute timer. The last payloads are compared like the GPU
  parity tests compare them (floats within 1e-4), and the answer is
  `{available, status: completed | mismatch | failed, parity, reason, cpu, gpu}` with the device,
  library, version and `trials_ms` of each engine. One comparison runs at a time; agent calls wait for
  the GPU worker as usual. The CPU service answers `{available: false, reason}`. The agent's sandbox may
  reach only `/mcp` (`agent/sandbox-policy.yaml`), so the route is the API's alone.
- **Data a pack may lack.** The news tools need a ticker-linked news table (`analytics.news_table`) and
  `intraday_scan` needs minute bars (`market.bars` with `frequency: 1min` in `pack.json`). A pack without them
  keeps every tool registered, so the registry, the sandbox policy and the UI do not change: those tools' MCP
  descriptions start with "Unavailable in the active data pack", the worker neither loads nor warms them up,
  and a call returns `status: "failed"` at once with `error.code` `news_unavailable` or
  `minute_bars_unavailable`. `synthetic-market`'s daily-bar profiles have no minute bars, and a real-market
  pack may have no news table.
- **Scope.** Every tool takes an optional `source_ids`, which the Hermes plugin sets to the run's selected
  sources. A market tool refuses (`source_not_selected`) when the pack's structured source is not among them.
- **Prediction.** `predict_asset_outcomes(template_id, asset_ids?)` runs one of the pack's curated PQL
  templates (`pack.json` → `prediction.templates`) with `kumo-relational-client`. It returns `available` /
  `reason`, `template_id`, `pql`, `anchor`, `horizon {value, unit}` (read from the PQL window), and
  `rows [{asset_id, probability}]`. It is registered only when both `KUMO_RELATIONAL_URL` and `KUMO_API_KEY`
  are set: the URL of a Kumo Relational service behind a key-checking proxy ([Kumo service](../../docs/kumo-service.md)),
  and its key, which the client sends as `X-API-Key`. Neither leaves the tool off; one without the other stops the
  server at startup with a message naming both. The server calls the service itself, outside the agent's sandbox,
  so the sandbox policy names no Kumo host. A prediction is one attempt with a 60 s timeout and no retries, so a
  slow service returns `available: false` before the agent's MCP timeout (180 s). Never install the client's `[explain]` extra: it sends raw cell values to a third-party
  LLM.

## Scale

Two tiers, so that a pack larger than the GPU still runs.

- **Daily tables** are loaded once, with only the columns the tools need. Before loading, the worker logs their
  row counts from the Parquet footers and an estimate of the memory the frames will take: measured on a
  2,000-issuer pack, about 225 bytes per daily price row on the CPU and 135 on the GPU, prices and anomaly
  features together (cudf keeps strings in Arrow columns). 10,000 issuers over 10 years, about 27 million
  rows, is about 3.6 GB of an A100's 40 GB. The GPU engine uses cudf.pandas' managed memory pool
  (`CUDF_PANDAS_RMM_MODE=managed_pool`), so a pack past the GPU's memory pages to host memory, more slowly,
  instead of failing.
- **The correlation graph** in `sparse_declared_peers` mode is computed from the declared pairs alone, joined
  on the date: its memory grows with the pairs, not with the square of the assets. On a 2,000-issuer pack
  (16,000 directed edges) that took the graph from 3.1 s to 0.73 s on the CPU and from 4.5 s to
  0.21 s on the GPU, with the same correlations (within 6e-16). `full_correlation` builds an assets x assets
  matrix, so keep it for small packs.
- **Minute bars** are never loaded whole. `intraday_scan` names the assets (or a universe) and a window, and
  [`bars.py`](src/market_analytics/bars.py) scans them in place from the pack's raw dataset
  (`pack.json` → `market.bars`, whose symbols are the pack's asset ids). It picks the files by symbol (one file per
  symbol) or by month (`month=YYYY-MM/` partitions), skips those whose Parquet footers show no row group in the
  window, and reads the rest in batches that stay under `MARKET_ANALYTICS_BATCH_BYTES`, estimated from the
  footers. A batch is one `read_parquet` call over its files, as Polars' `scan_parquet` takes a file list: cudf
  pays about 25 ms per call, so reading file by file was 16 to 46 times slower on the GPU. In month partitions
  the window and the symbols go into the reader, which skips row groups by their statistics. One-symbol files
  have no symbol column, so they are read whole, and each row's symbol follows from its file's row count. Each
  batch is reduced, for example to one row per symbol and session (`session_profile`), before the next is read, so
  memory holds one batch whatever the dataset's size. Peak GPU memory was 5 to 7 times the batch's estimate, and
  peak CPU memory 8 to 14 times, which is why the CPU's default batch is a quarter of the GPU's.
  `intraday_scan`'s reduction (`tools/intraday.py`) turns each batch into one row per asset and session: its
  bar, VWAP, the sum of squared minute returns, the deepest fall from the running high close, and the volume in
  the first and last 30 minutes. The ranking runs on those rows.

Minute-bar scans measured on an A100 VM, on a real-market minute-bar dataset (2,200 US symbols, 117 million minute bars, 3.75 GB of
uncompressed columns), reducing each symbol's regular sessions (09:30 to 16:00) to session bars. On both
engines the 572,995 session bars match a DuckDB rollup of the same files: prices and bar counts exactly, volumes
to within 1e-14. The last row scans 12 copies of every file under new names, which is more data than the GPU
holds. The copies are symlinks to the same files, so every read after the first came from the page cache: the
time leaves out the disk reads a real dataset of that size would need.

| Scan | Batches | CPU | GPU | Peak GPU memory |
| --- | --- | --- | --- | --- |
| All 2,200 symbols, 305 sessions, 2 GiB batches | 2 | 46.5 s | 2.9 s | 9.6 GB |
| The same with 256 MiB batches | 15 | 43.7 s | 3.8 s | 1.6 GB |
| The 50 largest symbols, 30 sessions | 1 | 1.14 s | 0.20 s | 0.8 GB |
| 12 copies: 26,400 files, 45 GB, 1.28 billion bars, 1 GiB batches (the GPU default) | 42 | 522 s | 30.2 s | 7.1 GB |

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_ACTIVE_DIR` | `/data/active` | The active data pack |
| `MARKET_ANALYTICS_ENGINE` | `cpu` | `gpu` installs cudf.pandas, cuml.accel and nx-cugraph in the worker (GPU image only) |
| `MARKET_ANALYTICS_TIMEOUT_SECONDS` | `120` | How long one call may run before the worker is replaced |
| `MARKET_ANALYTICS_BATCH_BYTES` | 1 GiB on the GPU, 256 MiB on the CPU | The most a minute-bar scan reads at once, estimated from the Parquet footers ([scale](#scale)). A CPU scan peaks at 8 to 14 times it |
| `CUDF_PANDAS_RMM_MODE` | `managed_pool` | cudf.pandas' memory: managed memory past the GPU's pages to host memory. Compose sets it explicitly |
| `KUMO_RELATIONAL_URL` | unset | The Kumo Relational service, an `https://` URL ([Kumo service](../../docs/kumo-service.md)). With `KUMO_API_KEY`, enables `predict_asset_outcomes` |
| `KUMO_API_KEY` | unset | The service's key, sent as `X-API-Key`. Compose passes it as the `kumo_api_key` secret (`/run/secrets/kumo_api_key`), which the server reads when the variable is unset |

## Run

```bash
# Locally, against a built pack
DATA_ACTIVE_DIR=/path/to/active uv run --project tools/market-analytics market-analytics-server

# CPU image (the default: ANALYTICS_EXTRAS=kumo)
docker build -t market-demo/market-analytics:local tools/market-analytics

# GPU image: RAPIDS 26.06 for CUDA 12 (driver 535 or newer) on Linux; run it with gpus: all and
# MARKET_ANALYTICS_ENGINE=gpu
docker build --build-arg ANALYTICS_EXTRAS=gpu-cu12,kumo -t market-demo/market-analytics:gpu tools/market-analytics
```

The console script is `market_analytics.bootstrap:main`. Start the server through it: the worker installs the
RAPIDS accelerators before anything imports pandas, which only works because the entry module imports nothing
heavy (a spawned process re-imports it first).

### cudf.pandas fallbacks

cudf.pandas falls back to pandas where it has no GPU path. Starting the worker does so four times: loading a
pack (`merge_asof`, and `rename_axis` for a `full_correlation` graph, once each) and reading the warm-up's window
(`Timestamp.to_pydatetime`, twice). So `CUDF_PANDAS_FAIL_ON_FALLBACK=1`, which turns the first fallback into an
error, stops the worker while it loads. The tool calls and minute-bar scans themselves do not fall back. To list
fallbacks, set `LOG_FAST_FALLBACK=1` (cudf.pandas writes them to `cudf_pandas_unit_tests_debug.log` in the working
directory) or run a call under `cudf.pandas.profiler.Profiler`.

## CPU and GPU timings

The Benchmark tab claims a GPU speedup only when every one of its CPU/GPU pairs favors the GPU and the two payloads
match. Large scans win: whole-universe daily scans, the anomaly scan, and minute-bar scans over hundreds of stocks.
Small calls (a few assets, a few sessions, a 2,000-node PageRank) cost less than the GPU's fixed overhead per
operation, so the CPU wins them.

Measured on a 40 GB A100 VM with 12 vCPUs, on the `qualification` profile of `market-analysis`, the pack
`synthetic-market` replaced at the same scale (2,000 issuers, 1.36 million daily bars, 84,000 news items). The
method: a throwaway container from the stack's own GPU image, with `--gpus all` and the pack mounted read-only, ran the service's worker with `MARKET_ANALYTICS_ENGINE=cpu` and then with
`gpu`, never both at once, while the rest of the stack sat idle. Each call ran once to warm up, then five timed
repeats. The time is the tool's own compute timer, the one receipts show. Median milliseconds:

| Call | CPU | GPU | CPU/GPU |
| --- | --- | --- | --- |
| `market_scan`, 2,000 issuers, 2024 to 2026, return and volatility | 229 | 77 | 3.0x |
| `market_scan`, 2,000 issuers, volume z-score | 228 | 82 | 2.8x |
| `market_scan`, 12 reviewed assets, 20 sessions | 86 | 56 | 1.5x |
| `market_anomaly_scan`, 12 assets | 76 | 41 | 1.8x |
| `market_anomaly_scan`, 2,000 issuers | 291 | 119 | 2.5x |
| `price_context`, 3 assets, 21 sessions | 70 | 33 | 2.1x |
| `sentiment_timeline`, 22,000 news items, weekly | 29 | 40 | 0.7x |
| `analyze_news_price_relationship`, 2,000 news items | 746 | 92 | 8.1x |
| `analyze_market_relationships`, top 10 | 37 | 37 | 1.0x |

Both engines returned the same results for every call (floats within 1e-4), here and for 26 other argument
shapes (filters, frequencies, horizons, empty and invalid requests, non-UTC offsets). On the GPU, the first
call of each shape after the worker reported ready took at most 270 ms (the 2,000-issuer anomaly scan, usually
about 145 ms; every other call about 100 ms or less), because the warm-up had paid the one-time cost: about
11 s, nearly all of it cudf.pandas compiling kernels for the first `market_scan`. Starting the worker, warm-up
included, took 25 s on the GPU and 10 s on the CPU.

`sentiment_timeline` is slower on the GPU and `analyze_market_relationships` breaks even. Their work is
small: the first groups 22,000 news rows into 11 weekly points, the second runs PageRank on 2,000 nodes and
sorts 16,000 edges. At that size the fixed cost of each GPU operation (launching kernels, waiting for them,
copying results back to the host) outweighs the arithmetic, which pandas and NetworkX finish in about 30 ms.

Before the [timestamp change](#how-it-fits) the GPU engine was slower on every call (0.1x to 0.9x; for
example 1,560 ms against 250 ms for the first `market_scan` row), because every operation on a frame with a
tz-aware column fell back to pandas. The CPU results did not change.

### `intraday_scan` on real minute bars

The same method on a pack built from a real minute-bar dataset (1,601 stocks, 117 million one-minute bars in 2,200
per-symbol files, 1.8 GB), with the bars on the VM's disk and read in place. The default batch budget (1 GiB)
applied. Bars counts those in the window's regular sessions; a per-symbol file is read whole, so a scan reads
all 15 months of each stock it names. Median milliseconds of five calls after one:

| Call | Bars | Batches | CPU | GPU | CPU/GPU |
| --- | --- | --- | --- | --- | --- |
| 2 stocks, 5 sessions, ranked by realized volatility | 3,910 | 1 | 85 | 216 | 0.4x |
| `top_50`, 9 sessions, ranked by intraday range | 175,923 | 1 | 1,180 | 291 | 4.1x |
| `liquid_500`, 48 sessions, ranked by drawdown | 8,685,591 | 2 | 12,261 | 1,048 | 11.7x |
| `all_assets`, all 298 sessions, ranked by intraday range | 98,760,895 | 4 | 72,937 | 7,543 | 9.7x |

Both engines returned the same payloads (floats within 1e-4). Two stocks is too little work for the GPU, as
with the small daily tools above. On the GPU the first call of each shape after the warm-up took at most 12%
longer than its median; starting the worker took 18 s on the GPU and 5 s on the CPU, warm-up included. On the
CPU a whole-market scan uses most of the 120 s deadline (`MARKET_ANALYTICS_TIMEOUT_SECONDS`).

The metrics divide columns with `ratio()` (a product with a power), not `/`: for a column divisor, cudf checks
for zeros with CuPy reductions that CuPy compiles once per array size class, about 9 s each on the A100. With
`/`, the first featured question after a restart took 9.4 s instead of 0.3 s, and any request that reduced to a
new number of rows could pay it again.

### Small universes

Each tool filters, groups and runs PCA on its engine, then ranks its per-asset or top rows on the host in NumPy,
which computes exactly what pandas computed (`tools/common.py`). On a real-market pack of 1,601 stocks, measured
through `POST /benchmark` on an A100, a 50-stock `market_scan` still ran faster on the CPU (about 0.8x): what is left
on the GPU is about 25 pandas calls (the filter, the session choice and the groupby), each with a fixed cost of 0.5 to
1.5 ms, against 23 ms of pandas. Scans of every stock over a long window ran 2.5x to 2.7x faster on the GPU. No call
fell back to pandas: cudf.pandas logged no fallback and `CUDF_PANDAS_FAIL_ON_FALLBACK=1` passed.

The memory mode matters at this size too. In the isolated method the 50-stock scan took 29 ms on the GPU with
`CUDF_PANDAS_RMM_MODE=managed_pool` and 15 ms with `async`. The stack keeps the managed pool, which lets a pack
larger than the GPU's memory page to host memory instead of failing ([scale](#scale)).

### `market_anomaly_scan` on the device

Through cudf.pandas and cuml.accel the anomaly scan took about 80 ms on an A100 against 99 ms on the CPU, because it
spent its time moving data: filtering the feature frame by universe and window took 27 proxied pandas calls, both windows
were copied to the host, the NumPy standardizing and medians there ran about twice as slow in the GPU process as in the
CPU one, and every PCA input and output was converted between host and device. On the GPU engine
(`src/market_analytics/tools/anomaly_gpu.py`) each universe's features stay on the device as CuPy arrays (built once at
warm-up, in `MarketData.resident`), a window is two comparisons on an int64 timestamp column, and standardizing, cuML's
PCA (full SVD solver, CuPy in and out), the 95th percentile, the ranking and the median/MAD z-scores stay there; only the
ranked rows return, for their ids and timestamps. The CPU engine's path is unchanged and the two share one result type
and one payload builder. The engine reports library `cuml`, not `cuml.accel`.

Measured with `POST /benchmark` on a real-market pack, median CPU/GPU over five pairs, the same call before and
after, scoring July 2025 to January 30, 2026 against a first-half 2025 baseline:

| Universe | A100 before | A100 after | B200 host after (CPU 2.5x faster) |
| --- | --- | --- | --- |
| 1,000 most liquid | 1.04x to 1.35x | 4.1x to 4.3x (GPU 28 ms) | 4.2x to 4.3x (GPU 11 ms) |
| 500 most liquid | 1.0x | 2.6x to 3.2x | 2.8x to 2.9x |
| every stock | 1.5x to 2.1x | 7.7x to 10.4x | 5.9x to 7.2x |
| 50 most liquid | 0.7x | 1.0x to 1.5x | about 1.0x |

The ranked sessions are identical to the host path, the scores agree to about 2e-15 relative and the deviations to the
bit.

## Changing the contract

A new optional column is a minor change. Renaming or removing a required column, or changing its type, needs
a new contract id (`market-analytics/v2`), because packs declare the contract they satisfy.
