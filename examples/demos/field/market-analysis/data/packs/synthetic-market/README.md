# synthetic-market pack

A fictional US equity market, made with [NeMo Data Designer](https://github.com/NVIDIA-NeMo/DataDesigner) and
Nemotron, in the same pack format a real market dataset uses. It needs no key, no download and no private data,
so it is the default. Real SEC EDGAR filings and eCFR Title 17 sit
beside it as separate document sources. Every issuer, price and news item in `market_data` is fictional; none
of it is investment advice.

## What is in it

| Source | Kind | Origin | Contents |
|---|---|---|---|
| `market_data` | structured | generated, synthetic | Fictional issuers, their daily prices, declared peers and ticker-linked company news, in DuckDB and Parquet, with leakage-safe prediction views; in the 1-minute profiles, minute bars that `intraday_scan` reads in place |
| `sec_filings` | documents | downloaded, real | SEC EDGAR 8-K and 6-K filings from 2026 Q2 (1,000 pinned filings) |
| `market_regulations` | documents | downloaded, real | eCFR Title 17 as of 2026-08-17, and the SEC's 2023 cybersecurity disclosure rule (88 FR 51896): its summary and amended form text, Form 8-K Item 1.05 and its deadline among them |

The filings are real companies and the issuers are fictional, so the two never mix: no filing is turned into
news, and questions that use both keep them apart.

Profiles (`DATA_PACK_PROFILE`), the scale knob:

| Profile | Issuers | Window | Bars | `daily_prices` | Text |
|---|---|---|---|---|---|
| `ci` | 12 | 2026-06-01 to 2026-08-31 (64 sessions) | 1 minute (300,288) | 768 | committed |
| `interactive` | 50 | from 2025-01-02 (416 sessions) | daily | 20,800 | committed |
| `standard` (default) | 2,000 | from 2024-01-02 (668 sessions) | daily | 1,336,000 | committed |
| `intraday` | 500 | from 2026-03-02 (127 sessions) | 1 minute (24,828,500) | 63,500 | committed |
| `large` | 10,000 | from 2016-01-04 (2,680 sessions) | daily | 26,800,000 | `data generate --profile large` |

`large` needs more text than is committed: `./scripts/demo.sh data generate --profile large` extends `text/` in
place (the first 2,000 issuers stay as they are), and `./scripts/demo.sh up` rebuilds the data image with it.
Measured on a laptop, its build takes about 80 s and peaks at about 11 GB of memory (1.3 million news items,
a 4 GB build and a 1 GB raw dataset).

## Questions

`questions.yaml` holds eleven questions; six are featured and need only the default profiles: market leaders
among the 12 story issuers, news sentiment against the following returns, unusual sessions, the peer network, the
Form 8-K Item 1.05 rule with the one Q2 2026 filing in the sample that reports an incident, and negative company
news beside real filings about operational disruptions (kept apart: the issuers are fictional). The intraday
question needs a 1-minute profile (`ci` or `intraday`). The planted facts give every analytics question a known
answer, and the pinned corpus holds the documents a retrieval answer should cite.
Each question declares the `tools` it is expected to use, as the demo scenario picker's pills
([data packs](../../../docs/data-packs.md#sources-capabilities-and-questions)). The picker offers all eleven
(`examples`), the first five showing every tool; a build offers those of its profile.

### Recorded answers

`recordings/` holds the ten questions of the `standard` profile, recorded on 2026-10-01 with the `.env.example`
models (Nemotron 3 Ultra alone, on build.nvidia.com) and reviewed against the planted facts and the evidence. Four
were wrong and asked again, twice at most.

On 2026-10-02 five were recorded again the same way, after the fixes to the news tools' coverage, to evidence
citations, to Auto Ontology's return convention and to the regulations source (which now holds the SEC's 2023
cybersecurity rule): `news-and-filings`, `story-event-context`, `sector-sql`, `cyber-disclosure-rules` and
`peer-network`. `story-event-context` (all 23 items, with the five of 2026-08-28 marked as having no two-session
return) and `peer-network` now match the expected answers.

Later that day three were recorded again the same way, once Auto Ontology's seed imported a changed model again
(so the pack's return convention reached its catalog) and filing searches stopped naming the form:
`sector-sql`, `cyber-disclosure-rules` and `news-and-filings`. `sector-sql` matches the expected answer in every sector, and
`cyber-disclosure-rules` gives Item 1.05's content and the four-business-day deadline from Form 8-K General
Instruction B.1, with CB Financial's Item 1.05 filing. `news-and-filings` was asked three times, and the bundle
keeps the third. What remains:

- `news-and-filings` (featured) names GIOR and KASI with four negative items each, as expected, but gives
  each one's mean five-session return after all of its articles as the reaction to its negative ones. Its filing
  searches now describe the event in a sentence and retrieve one of the filings that report a disruption
  (Centerra Gold's 6-K), which the answer names. The passages they return are the filings'
  lists of risks, though, and the answer calls them all risk language and says the documents hold no filing that
  announces a disruption, while the corpus holds some (a TotalEnergies 6-K on production shut down in the Gulf,
  for one). Of the other two attempts, one named only a cybersecurity 8-K and the other searched for the category's
  name alone.

## How it is made

Two stages, so a build is deterministic and offline while the text still comes from Nemotron:

1. **`demo.sh data generate`** ([`data/generate`](../../generate/README.md)), run rarely. A seeded roster of
   issuer slots (industry, exchange, invented name root, ticker) goes to Data Designer as its seed dataset;
   Nemotron writes each company's name and profile, 12 headline templates per event type and sentiment, and
   the 12 story items. Names and tickers are checked against SEC's ticker lists, and invented roots and
   tickers against a list of unfit words. The result is `text/`, committed and reviewed like code, with
   `checks.json` recording the checks and the cost.
2. **`demo.sh data prepare`** runs `generator/build.py`: seeded numpy streams with the constants in
   `generator/model.yaml` make the prices, volumes and news timing, and write a raw dataset (month-partitioned
   bars, `companies.parquet`, `news.parquet`, a manifest) in the layout a real dataset uses. The market
   importer then builds the tables from it exactly as it does for a real dataset.

Every random draw comes from `numpy.random.default_rng([seed, crc32(stream), *keys])`, with one stream per
concern and issuer slot. The market is simulated over 2016-01-04 to 2026-08-31 and each profile keeps its
window, so an issuer has the same prices and news in every profile that includes it.

## What is planted

- **Prices**: daily returns are `beta * market + gamma * industry + drift + volatility * noise`, each factor
  Student-t with 4 degrees of freedom. Issuers in one SIC industry share a factor, so declared peers (the most
  liquid issuers with the same SIC code) are correlated. There are no splits or dividends.
- **The 12 story issuers** (slots 0 to 11) trade far more than any other, so they are always the 12 most liquid:
  the `top_12` universe and the prediction population. Each has one story item between 2026-08-17 and
  2026-08-28 (`model.yaml`, `story`) that moves its publication session by 6 to 12 percent, plus 30 percent of
  that over the next five sessions.
- **Background news**: about one item per issuer a month (three for story issuers), with an event type and a
  sentiment label, published between 11:00 and 20:30 UTC on a session day. Each moves the next session's return
  by 0.3 of the issuer's volatility with its sentiment, a modest relationship that
  `analyze_news_price_relationship` finds. Its headline is a seeded template filled with the company's name.
- **Volume** rises with the size of the day's move and is 2 to 4 times higher on news sessions.
- **Minute bars** (`ci`, `intraday`): 391 per session, 09:30 through the 16:00 closing auction, from a Brownian
  bridge between the day's open and close. Their rollup is exactly the daily bar.
- **Prediction**: the anchor is 2026-08-24 21:00 UTC with a five-session horizon; the views expose only what was
  observable then.

Labels: `assets.is_synthetic` is true, news comes from "Synthetic Newswire", the source is marked synthetic in
the UI, and the disclaimer says the data is fictional.

## How it was made

The committed text is for the `standard` profile (2,000 issuers), generated on 2026-09-29 with Data Designer
0.9.3 and `nvidia/nemotron-3-super-120b-a12b` on build.nvidia.com: 2,060 model calls, 566,403 input and
103,191 output tokens, in 27 minutes. A second run the same day renamed 39 issuers in 39 calls: 25 whose
invented root or ticker spelled an unfit word, and 14 whose ticker moved because of them. `text/checks.json`
records the latest run.

## Files

| File | Purpose |
|---|---|
| `pack.yaml` | the manifest (see `data/README.md`) |
| `schema.sql`, `views/prediction.sql` | the DuckDB schema (the real-market tables plus `company_news`) and the prediction views |
| `ontology.yaml` | table and column descriptions for Auto Ontology |
| `questions.yaml` | the demo questions; `featured` ones are the landing page and replay set, `examples` the picker's |
| `generator/build.py`, `generator/model.yaml` | the seeded market and its constants |
| `text/` | the Nemotron text and its checks (`demo.sh data generate`) |
| `corpus/*.manifest.json` | the pinned eCFR snapshot, Federal Register rule and EDGAR filings (URL and SHA-256 of every file) |
| `recordings/` | the replay bundle of the ten questions of the `standard` profile (`demo.sh record --all` with the `.env.example` models, [recordings](../../../docs/data-packs.md#recordings)); `intraday-ranges` needs a minute-bar profile |

## Licenses

- Synthetic data and its Nemotron-written text: Apache-2.0.
- eCFR: United States government public information. The eCFR is authoritative but is not the official legal
  edition of the CFR.
- SEC EDGAR: SEC website reuse terms; redistribution unknown. Filings are fetched at prepare time and never
  committed; issuer-authored content may carry separate rights.
