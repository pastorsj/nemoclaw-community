<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Models and routing

Hermes sends every model call to Switchyard as the route `market-research`. `SWITCHYARD_ROUTES` in `.env`
picks how Switchyard serves that route, and `AGENT_*_MODEL` pick the models. How the templates, the judge and
the latch work is in [`infra/switchyard/README.md`](../infra/switchyard/README.md). This page records which
combination is the default and why.

## Recommendation

**On build.nvidia.com (the default), Nemotron 3 Ultra alone: `SWITCHYARD_ROUTES=passthrough.nemotron`**, with
`nvidia/nemotron-3-ultra-550b-a55b` as the efficient model and Nemotron 3 Super (thinking off) for Hermes'
auxiliary and fallback calls. `.env.example` ships this. build.nvidia.com serves no GPT model, so the choice
there is between Ultra alone and the all-Nemotron escalation (Super answers, Ultra takes over). In the
2026-09-30 bake-off (17 questions over a synthetic and a real-market pack, two runs each), Ultra alone passed 13 of 34 runs and
Super → Ultra 12. Super → Ultra was also twice as slow at the median (140 s against 72 s), failed 4 jobs
against none, and left 6 of its 30 reports without a citation (Ultra alone: 4 of 34).

**With a provider that serves a frontier model, let Nemotron 3 Ultra lead and escalate to GPT-6.1 Sol:
`SWITCHYARD_ROUTES=escalation.nemotron-gpt`**, with GPT-6.1 Sol as the capable model and as the escalation judge
(under a second id at that provider), Nemotron 3 Super for auxiliary and fallback calls, and `CAPABLE_BASE_URL`
and `CAPABLE_API_KEY` pointed at the provider ([configuration](configuration.md#1-inference-endpoint)). In the
2026-10-01 frontier bake-off ([below](#the-2026-10-01-frontier-bake-off)) it passed 10 of 16 runs with the
primary grader and 14 with the second, ahead of GPT-6.1 Sol pinned (7 and 9) and Claude Opus 5.5 pinned (9 and
13), while GPT-6.1 Sol served 10% of the agent turns and, with its judge calls, 20% of the tokens.

**Pinned frontier is the fallback.** Claude Opus 5.5 pinned (`pinned-capable.nemotron-claude`) was the stronger
and steadier pinned arm (p95 72 s, no tool errors). GPT-6.1 Sol pinned lost runs to repeated tool-argument errors
and two stalled streams ([below](#the-2026-10-01-frontier-bake-off)). On 2026-09-30, GPT-6 Sol pinned had beaten
Ultra → Sol with a Nemotron 3 Super judge (24 against 20 of 34); a frontier judge reversed that.

**What to use where** (the two bake-offs used different question sets and endpoints, so compare within a date):

| Situation | `SWITCHYARD_ROUTES` and models | Passed |
|---|---|---|
| build.nvidia.com (default) | `passthrough.nemotron`, efficient Nemotron 3 Ultra (`nvidia/nemotron-3-ultra-550b-a55b`) | 2026-09-30: 13 of 34; 2026-10-01: 7 of 16 |
| A provider serving GPT-6.1 Sol (recommended) | `escalation.nemotron-gpt`: Ultra → GPT-6.1 Sol, judged by GPT-6.1 Sol | 2026-10-01: 10 of 16 |
| A provider serving Claude Opus 5.5 | `pinned-capable.nemotron-claude`, capable Claude Opus 5.5 on `CAPABLE_BASE_URL` | 2026-10-01: 9 of 16 |
| GPT-6.1 Sol on every turn | `pinned-capable.nemotron-gpt` | 2026-10-01: 7 of 16 (GPT-6 Sol on 2026-09-30: 24 of 34) |
| Ultra escalating to Claude Opus 5.5 | `escalation.nemotron-claude`, judged by Claude Opus 5.5 | 2026-10-01: 8 of 16 |
| All-Nemotron escalation on build.nvidia.com | `escalation.nemotron`: Super → Ultra, 3.5 Lightning judge (the commented block in `.env.example`) | 2026-09-30: 12 of 34 |

Switching is an `.env` edit plus `./scripts/demo.sh restart switchyard`; the sandbox is not rebuilt.

**One OpenAI-compatible endpoint for every model.** If your organization runs an OpenAI-compatible gateway
that serves both Nemotron and GPT models, there are two ways to use it:
- *Only for the capable model* (as the 2026-09-30 bake-off did): keep `INFERENCE_BASE_URL` on build.nvidia.com and set
  `CAPABLE_BASE_URL` and `CAPABLE_API_KEY` to the gateway.
- *For every model* (as the 2026-10-01 bake-off did): set `INFERENCE_BASE_URL` and `INFERENCE_API_KEY` to the gateway, leave `CAPABLE_*` empty,
  use the model ids its `GET /v1/models` lists, and give the retriever its own build.nvidia.com key
  (`RETRIEVER_API_KEY`).

Either way the gateway's URL and key live only in your `.env`, and `./scripts/demo.sh doctor --keys` checks
that every model id the template uses is listed.

### The default on build.nvidia.com

`synthetic-market`'s committed replay bundle was recorded with this default: Ultra alone on build.nvidia.com, every
profile, on a Brev A100 VM. Its ten questions were recorded on 2026-10-01 and five of them again on 2026-10-02, and
every answer was reviewed against the built pack and its evidence. How the bundle was made, and which faults
remain, is in [data packs](data-packs.md#recordings) and the pack's README.

The retired `market-analysis` pack's bundle was recorded the same way on 2026-09-29, in four `record` runs of
the featured questions (three of all six, one of two). What they showed:

- **Citation tokens.** Ultra there often writes `【evidence:<id>】` or `【hermes-receipt:<id>】` instead of
  `[evidence:<id>]`, or names the receipt in prose. Before the API accepted the bracketed forms, 4 of 5
  answers in the first run had no citations. In that bundle 4 of 5 were cited; the Event Reaction answer named
  its receipts in a prose "Sources" line, which does not count.
- **Event Reaction is unstable, as H1 was.** Of four attempts, two listed the eight planted events and their
  returns through Auto Ontology. One reached the Hermes run deadline after asking Auto Ontology to read a
  spilled-over tool result, and one reported the 2,000-issuer short-form news stream instead of the planted
  events, without the publication-session return.
- **Market Leaders** scanned from August 1 in every run: 21 sessions, not 20. The strongest and weakest
  assets match the database (Aether, Cascade), but Aether's +0.21% is not its 20-session −0.94%.
- **Large-Universe Scan** names the database's strongest and weakest issuers but misreads the unit: the tool's
  returns are fractions (4.19 is +419%), which that bundle's answer labeled "×" and another run showed as
  "+4.191 %".
- **Cybersecurity Rules** made one retrieval and covers Regulation S-K Item 106 only. It does not mention
  Form 8-K Item 1.05, which the earlier recording with GPT-6 Sol did.
- The anomaly and Galena answers matched their receipts in every run. Auto Ontology took 68 to 186 s per
  query with Super and 3.5 Lightning on build.nvidia.com, and 332 s on each unanswerable request.

### Tuning Ultra for the current packs

On 2026-09-30 the featured questions of a synthetic and a real-market pack ran with Ultra alone on
build.nvidia.com, on a Brev A100 VM, before and after tuning: 14 runs before (one failed), 30 after, unjudged. What they showed, and what changed:

| Gap | Cause | Change | After |
|---|---|---|---|
| A job failed after 5 minutes of "Service temporarily overloaded" | build.nvidia.com sends the error inside an HTTP 200 stream, and Switchyard 0.3.0 passes it through | Hermes falls back to `market-research-fallback` (Super) after two failed tries ([how](../infra/switchyard/README.md#routes)) | A simulated overload finished on Super in 15 s |
| Universes the pack does not have (`top_50` on `synthetic-market`), or `all_assets` and Auto Ontology detours on the real-market pack | Hermes lists the tools once, when the sandbox starts, and a `DATA_PACK` switch kept the sandbox, so the model saw the previous pack's universes | A pack switch recreates the sandbox ([OpenShell](openshell.md)) | No invalid universe on a fresh sandbox |
| "The 20 sessions ending August 31" scanned from August 3: 21 sessions instead of 20 | The model does not count trading days, even with a worked example | `market_scan` takes `sessions=20` with `end` | Both packs' Market Leaders match the database |
| Anomaly deviations shown as percentages | Nothing said they are robust z-scores | The tool description and the result's limitation say so | No later run showed them as percentages |
| A ranking sent to Auto Ontology, which returned dollar price changes as "returns" | The routing table did not say where leaders and laggards go | `SOUL.md` sends rankings over any window to the market tools | Moves and Filings matches the database |
| Receipts named in prose ("evidence `hermes-receipt:<id>`") instead of cited | Ultra rarely writes the `[evidence:<id>]` token as instructed | The API counts a receipt id in code or bold as a citation | 20 of 30 answers were cited before this change; 27 of 30 would have been |

Ultra loaded no skill in any of these runs, although `SOUL.md` asks it to before a tool's first call. Guidance
that must reach it therefore lives in `SOUL.md` and the tool descriptions, not only in the skills.

Still open:
- **Form 8-K Item 1.05** searches 7 to 12 times for the four-business-day deadline, which the eCFR sections in
  the corpus do not state, then says so. A "search each topic once" line in the tool description did not
  change it.
- Ultra reads "which had the strongest and weakest returns" as one of each.
- Scope slips vary by run, for example news for every issuer instead of the 12 most liquid.

The escalation judge's prompt now escalates only for a failure it can name: a stuck or invalid tool loop, an
error used as data, or a report with a missing citation, a wrong window or unit, an unanswered part, a
contradiction or no evidence. The bake-off below measures it ([the tuned judge](#the-tuned-judge-measured)).

## Models and ids

| Role | Model | build.nvidia.com id (`https://integrate.api.nvidia.com/v1`) |
|---|---|---|
| Efficient (default: every turn) | Nemotron 3 Ultra 550B-A55B | `nvidia/nemotron-3-ultra-550b-a55b` |
| Auxiliary and fallback calls (`AGENT_AUX_MODEL`), thinking off | Nemotron 3 Super 120B-A12B | `nvidia/nemotron-3-super-120b-a12b` |
| Escalation judge in `*-gpt` and `*-claude` (`AGENT_JUDGE_MODEL`) | a frontier model from the capable provider: GPT-6.1 Sol or Claude Opus 5.5, under a second id there | not served |
| Capable (escalation and pinned templates) | GPT-6.1 Sol (GPT-6 Sol in the 2026-09-30 bake-off), over the Responses API | not served; the id your provider lists, e.g. `gpt-6.1-sol` |
| Capable (`*-claude` templates) | Claude Opus 5.5, over the Anthropic Messages API | not served; the id your provider lists, e.g. `claude-opus-5-5` |
| Judge (and aux) for the all-Nemotron escalation | Nemotron 3.5 Lightning 30B-A3B | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| Candidate efficient model | Nemotron 3.5 Super | to be evaluated once it is served publicly |

On any other OpenAI-compatible endpoint, use the ids its `GET /v1/models` lists; `./scripts/demo.sh doctor
--keys` checks every id the template uses. Retrieval always uses the retriever endpoint (build.nvidia.com):
`nvidia/nemotron-3-embed-1b` and `nvidia/llama-nemotron-rerank-vl-1b-v2`.

## The 2026-10-01 frontier bake-off

**Question.** Does Nemotron 3 Ultra with frontier escalation get close to frontier quality at lower frontier
usage? Only the Nemotron-led arms are deployment candidates; the pinned frontier arms are reference ceilings.

**Setup.** A real-market pack (real US equity prices, which this repository does not ship) on the same kind of
A100 VM and stack as below, every profile. Every model came from an OpenAI-compatible inference gateway; the
retriever stayed on build.nvidia.com. The 8 real-market questions of the 2026-09-30 bake-off ran twice per arm (pass 2 in reverse arm order): 80 runs, one at a time, in
1 h 34 min. `SWITCHYARD_CONFIRMATIONS=1`; the tuned judge prompt; Nemotron 3 Super for auxiliary and fallback
calls. Each escalation arm was judged by its own frontier model under a second id at the gateway (Switchyard keys
a route's targets by model id; [why](../infra/switchyard/README.md#routes)).

**Scoring.** Deterministic checks of each answer against values computed from the built database, and two
answer graders on every run, three samples each with a majority vote: GPT-6 Sol (the 2026-09-30 grader and
prompt) and Claude Opus 5.5 (the same prompt). A run passes when its deterministic check passes and the
grader's majority says pass. The first scoring failed three Claude-written Form 8-K reports that said, in words
the check did not know ("can't cite ... the filing deadline", "neither is stated here"), that the evidence lacks
the deadline. The check now accepts any such flag about the deadline and still fails any other deadline; the
results below are the re-scored ones (one more pass for Claude Opus 5.5 pinned, two for Ultra → Claude Opus 5.5).

| Arm | Role | Pass, GPT-6 Sol grader | Pass, Claude Opus 5.5 grader | Frontier share of agent turns | Frontier share of tokens, without / with judge calls | Escalated (false latches, rescues) | p50 / p95 s | Failed jobs, stalled runs |
|---|---|---|---|---|---|---|---|---|
| Ultra alone (`passthrough.nemotron`) | candidate | 7/16 | 10/16 | 0% | 0% | – | 16 / 120 | 0, 0 |
| GPT-6.1 Sol pinned (`pinned-capable.nemotron-gpt`) | ceiling | 7/16 | 9/16 | 100% | 100% | – | 32 / 617 | 2, 2 |
| Claude Opus 5.5 pinned (`pinned-capable.nemotron-claude`) | ceiling | 9/16 | 13/16 | 100% | 100% | – | 28 / 72 | 0, 0 |
| **Ultra → GPT-6.1 Sol (`escalation.nemotron-gpt`)** | candidate | **10/16** | **14/16** | 10% | 10% / 20% | 5/16 (1, 2) | 52 / 104 | 0, 0 |
| Ultra → Claude Opus 5.5 (`escalation.nemotron-claude`) | candidate | 8/16 | 9/16 | 13% | 23% / 35% | 5/16 (1, 3) | 56 / 156 | 0, 1 |

Tokens are Switchyard's per-session counts over every upstream call. A false latch is an escalated run of a
question Ultra alone passed both times; a rescue is an escalated run that passed on one it did not.

What it shows:
- **Ultra → GPT-6.1 Sol was the best arm with both graders**, above both pinned ceilings, with every deterministic
  check passed and no failed or stalled job. Its GPT-6.1 Sol calls, capable and judge together, used 255k input
  tokens over the 16 runs, half of Sol pinned's 511k. The judge's own share was 124k input and 3.6k output tokens.
- **The judge adds seconds, not minutes.** The GPT-6.1 Sol judge took 2.9 s per verdict at the median and 6.0 s
  at p95, about 10 s per run. The Claude Opus 5.5 judge took 4.5 s and 11.5 s; one verdict hit its 120 s deadline
  and the turn was retried.
- **GPT-6.1 Sol pinned was held back by the tools and the stream, not by its answers.** It sent `market_scan` a
  `start` and a `sessions` together, which the tool rejects, and repeated the call (23 tool errors; the rejection
  now names the argument to drop and gives the call to make), and 2 of its 16
  runs failed on a stalled stream at 617 s, as on 2026-09-30. With escalation, Ultra drives the market tools and
  GPT-6.1 Sol joins late, so neither happened.
- **Ultra → Claude Opus 5.5 did no better than Ultra alone** (8 and 9 against 7 and 10). The handoff to Claude
  worked in every escalated run, but the Claude judge left more of Ultra's flawed reports alone (rankings out of
  order, wrong dates).
- **The graders agreed on 66 of 80 runs.** The Claude grader passed more runs in every arm (1 to 4 more). Both
  ranked Claude Opus 5.5 pinned above GPT-6.1 Sol pinned, so the GPT-6 Sol grader showed no preference for its
  own model family.

Limits: 16 runs per arm on one pack, so a difference of one or two runs is noise; the gateway's Ultra rather than
build.nvidia.com's; the pinned Sol arm's result depends on the `market_scan` argument rule above.

## How the bake-off was run

**When and where.** 2026-09-30, after the tuning above, on a Brev VM with one A100 (40 GB) and every profile
(`core,retrieval,analytics-gpu,kumo,ontology`: the GPU market tools, the Kumo NIM, then in the demo stack, and Auto Ontology),
OpenShell 0.1.2, Hermes v2026.9.24 and Switchyard 0.3.0. First `synthetic-market`, then a real-market pack,
each with a sandbox started for that pack.

**Endpoints.** The Nemotron arms ran on build.nvidia.com, as the default does. GPT-6 Sol came from an
OpenAI-compatible gateway through `CAPABLE_BASE_URL` and `CAPABLE_API_KEY`, while the Nemotron side of those
arms stayed on build.nvidia.com.

**Arms.** Models: Ultra is Nemotron 3 Ultra 550B-A55B, Sol is GPT-6 Sol, Super is Nemotron 3 Super
120B-A12B, and Lightning is Nemotron 3.5 Lightning 30B-A3B.
Every arm used `SWITCHYARD_CONFIRMATIONS=1`.

| Arm | `SWITCHYARD_ROUTES` | Efficient | Capable | Judge and auxiliary calls | Endpoints |
|---|---|---|---|---|---|
| A0 Ultra alone | `passthrough.nemotron` | Ultra | – | Super (auxiliary only) | build.nvidia.com |
| A1 Sol pinned | `pinned-capable.nemotron-gpt` | – | Sol | Super (auxiliary only) | Sol on the gateway, Super on build.nvidia.com |
| A2 Ultra → Sol | `escalation.nemotron-gpt` | Ultra | Sol | Super, with the tuned judge prompt | Sol on the gateway, the rest on build.nvidia.com |
| A4 Super → Ultra | `escalation.nemotron` | Super | Ultra | Lightning, with the tuned judge prompt | build.nvidia.com |

**Questions.** Each pack's six featured questions plus `outcome-prediction` and `large-universe-scan`, and on
`synthetic-market` also `story-event-context` ([data packs](data-packs.md)): 9 and 8 questions, each sent
with its own `sources`, as the UI's cards send them. Every question ran twice per arm: pass 1 in the order
A0, A1, A2, A4 and pass 2 in reverse, so a busy hour on an endpoint does not land on one arm. That is
17 questions × 4 arms × 2 = 136 runs, one at a time.

**Procedure.**
1. Point `.env` at the arm (the table above) and run `./scripts/demo.sh restart switchyard`. That re-renders
   the routes, clears latches and `/v1/stats`, and refreshes the API's model ids. The sandbox is untouched.
2. Submit each question through `POST /v1/jobs/async/submit` and wait for it to finish. Every job is a fresh
   Hermes session, so every question starts on the efficient model.
3. Save the job's export (`GET /v1/jobs/async/job/{id}/export`: report, `execution.v2` events, receipts) and
   Switchyard's per-session stats (`GET /v1/routing/session-stats?session_id=<job id>`, which include the
   judge's calls).
4. Score every run: a deterministic check and an LLM judge (below).

**Scoring.** Each run got a deterministic check, which needs the job to succeed, the question's tool to be
called, at least one citation, and the answer's key figures (the strongest and weakest assets, a mean return,
a named filing) to match values computed read-only from the active DuckDB; and an LLM judge, GPT-6 Sol on the
gateway, three independent samples per run with a majority vote, blind to the arm and the model. A run passes
when both do. The columns below:

- *Judge pass*, *Judge mean*: the judge's majority verdict, and its mean 1-to-5 score over the arm's runs.
- *Grounded %*: the share of the report's claims the judge found supported by the receipts.
- *Tool calls, errors, duplicates*: calls that reported an error or produced a failed receipt; duplicates
  repeat the same tool with the same arguments.
- *Escalated*: any agent turn was served by the capable model. A *false latch* is an escalated run of a
  question that Ultra alone (A0) passed both times; a *rescue* is an escalated run that passed on a question
  A0 did not pass both times.
- *Fallback turns*: agent turns served by `market-research-fallback` after the agent's model was overloaded.
- *Latency*: submit to completion, as the API records it (one job at a time, so no queueing).

## Results

| Arm | Pass | synthetic | real | Deterministic | Judge pass | Judge mean | Grounded % | Tool calls / errors / duplicates | Escalated | False latch | Rescue | p50 s | p95 s | Failed jobs | Median input tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 Ultra alone | 13/34 | 7/18 | 6/16 | 28/34 | 16/34 | 3.31 | 97 | 108 / 5 / 0 | – | – | – | 72 | 329 | 0 | 29,841 |
| **A1 Sol pinned** | **24/34** | **13/18** | **11/16** | 25/34 | **28/34** | **3.93** | 97 | 260 / 12 / 96 | – | – | – | **60** | 617 | 4 | 25,404 |
| A2 Ultra → Sol | 20/34 | 11/18 | 9/16 | 24/34 | 23/34 | 3.68 | 97 | 180 / 5 / 55 | 10/34 | 2 | 5 | 80 | 409 | 1 | 35,222 |
| A4 Super → Ultra | 12/34 | 8/18 | 4/16 | 23/34 | 13/34 | 2.85 | 100 | 95 / 4 / 0 | 18/34 | 2 | 6 | 140 | 489 | 4 | 28,442 |

Served models (agent turns over the arm's 34 runs; Switchyard's upstream calls add the judge's):

| Arm | Agent turns served by | Upstream calls, judge included | Latch turn (escalated runs) |
|---|---|---|---|
| A0 | Ultra 145, Super 2 (fallback) | Ultra 145, Super 2 | – |
| A1 | Sol 219 | Sol 219, Super 1 | – |
| A2 | Ultra 91, Sol 93, Super 2 (fallback) | Super 100, Sol 93, Ultra 91 | turn 1 in 5 runs, 2 in 1, 3 in 2, 6 in 1, 7 in 1 |
| A4 | Ultra 64, Super 41, Lightning 16 (fallback) | Lightning 77, Ultra 64, Super 41 | turn 0 in 8 runs, 1 in 5, 2 in 5 |

Per question, one letter per run: `P` pass, `f` fail, `d` the deterministic check failed, `*` escalated,
`~` at least one turn on the fallback route.

| Pack | Question | A0 | A1 | A2 | A4 |
|---|---|---|---|---|---|
| synthetic | market-leaders | P~ P | P P | P P | fd~ P |
| synthetic | news-sentiment-reaction | fd fd | fd P | fd P* | fd* fd |
| synthetic | unusual-sessions | fd P | P P | fd P | fd* f* |
| synthetic | peer-network | P P | P P | P P | P P* |
| synthetic | cyber-disclosure-rules | f~ f | fd P | fd* P* | f* P* |
| synthetic | news-and-filings | f f | fd P | fd* fd~ | fd~ P* |
| synthetic | story-event-context | fd f | fd P | fd fd | f* fd |
| synthetic | outcome-prediction | f P | P P | P P | f* P |
| synthetic | large-universe-scan | f P | fd P | P P | P P |
| real | market-leaders | fd P | fd P | P P | fd~ fd~ |
| real | intraday-ranges | f P | P P | f f | fd P* |
| real | unusual-sessions | fd f | P P | P* P* | P* P* |
| real | peer-network | f f | f fd | fd P | fd P* |
| real | cyber-disclosure-rules | f f | fd P | fd* P* | fd f~ |
| real | moves-and-filings | P P | fd P | P* P* | f* f |
| real | outcome-prediction | f P | P P | f f | f* f* |
| real | large-universe-scan | f P | P P | fd P | f* f* |

What the numbers show:
- **Sol pinned is still the strongest arm**, as on 2026-09-29, and the margin held on both packs. Its
  failures were mostly bugs, not the model: of its 4 failed jobs, one ran out of tool calls because
  `market_scan` ignored `start` when `sessions` was also given, two lost a receipt to an Auto Ontology phrase
  longer than the receipt allows, and one waited on a stalled stream. Both bugs are fixed. The same
  `market_scan` bug is behind 87 of its 96 duplicate calls.
- **Ultra alone is the better build.nvidia.com choice.** It never failed a job, and its misses are in the
  report, not the tools: 4 of 34 reports had no citation, and the judge failed most of the rest for a wrong
  scope, window or claim, for example calling a strongly negative correlation a pair that "moved together".
- **Super → Ultra is slower and less reliable than Ultra alone.** Its judge escalated 18 of 34 runs, 8 of
  them at the first turn, so it mostly runs Ultra with extra calls; 6 of its 30 reports had no citation. Its
  fallback route serves 3.5 Lightning, and 2 of its 4 failed jobs ended on HTTP 404 errors from its model
  calls during a nine-minute burst.
- **Two questions were held back by the tools in every arm**, and both are fixed since ([after the
  fixes](#after-the-tool-fixes)). The news tools had no universe filter, so `news-sentiment-reaction` got all
  545 news items instead of the 12 most liquid issuers' 13; only three runs on Sol answered or flagged it
  correctly. The relationship tool did not say that its graph links declared same-industry peers or that a
  negative correlation means opposite moves, so on the real-market pack most reports called it a whole-market graph
  or listed an opposite-moving pair among those that "moved together".

### The tuned judge, measured

The tuned judge prompt ([above](#tuning-ultra-for-the-current-packs)) against the 2026-09-29 one (on the
retired `market-analysis` pack, one run per question), in A2:

| | 2026-09-29 | 2026-09-30, tuned |
|---|---|---|
| Runs escalated | 6 of 12 (50%) | 10 of 34 (29%) |
| Escalated runs that passed | 5 of 6 | 7 of 10 |
| Rescues | 2 | 5 |
| False latches, of A2's runs on questions A0 always passed | 3 of 6 (50%) | 2 of 6 (33%) |
| A2 passes against A1's | 6 against 11 | 20 against 24 |

The tuned prompt is more selective, and most of its escalations are worth it. It still escalates late in some
runs (turns 6 and 7, after Ultra had already searched or scanned several times), and 11 of the 24 runs it left
on Ultra still failed, mostly for report-level faults (a scope, a window, a misread number) that the judge,
which sees each message cut to 900 characters, rarely catches. The rule set before the first bake-off was to
adopt escalation only if it passes within one question of Sol pinned with false latches at 10% or less; it
meets neither.

### After the tool fixes

The news-universe, `market_scan` window and relationship-description fixes reached the VM after the
bake-off. The two questions they change then ran twice more on the two build.nvidia.com arms, graded the same
way (the tables above keep the bake-off's own runs):

| Pack | Question | A0 Ultra alone | A4 Super → Ultra |
|---|---|---|---|
| synthetic | news-sentiment-reaction | P P (was fd fd) | fd~ P* (was fd* fd) |
| synthetic | peer-network | P P (was P P) | P* P (was P P*) |
| real | peer-network | fd P (was f f) | P* P* (was fd P*) |

Each arm passed 5 of 6. Ultra's miss had no citation; Super → Ultra's was an overloaded turn that fell back
to Lightning, which replied with a plan instead of calling the tool. The other news runs reported the 12
issuers' 13 news items, and every real-market report said the graph links declared peers and kept the
opposite-moving pair apart from the pairs that moved together.

### Limits of this bake-off

- **Two runs per question.** A one-run difference on a question is noise: A0 passed 7 of its 17 questions
  once and failed them once. Across 34 runs the gaps between A1, A2 and A0 (24, 20, 13) are larger than that.
- **Sol judged Sol.** The judge is the A1 model. It was blind to the arm, graded against reference facts and
  the receipts, and voted over three samples, but self-preference cannot be ruled out. The deterministic
  checks do not favour Sol (A0 28/34, A1 25/34); the gap comes from the judge.
- **Receipts, not full passages.** The judge read the receipts, which cut retrieved passages at 1,500
  characters, and it was told so.
- **Receipts, not tool descriptions.** The judge never sees what the tools tell the model. On `peer-network`
  it first counted "PageRank" and "daily-return correlations", which the relationship tool's description
  states, as unsupported, in every arm. That question's reference facts now quote the description, and its runs were
  graded again; the tables use the second grades, which added 2 passes to A2 and 1 to A4.
- **Stalled streams.** Model streams sometimes ended early, from the gateway's Sol and from build.nvidia.com's
  Nemotron models alike. Switchyard logged the early end within a second, but in at least four runs Hermes,
  behind the OpenShell proxy, saw the connection close only 5 to 10 minutes later and retried (three on Sol in
  A1, one on Lightning in A4). This inflates the p95 of the affected arms more than their pass rates.
- **Endpoint load.** build.nvidia.com answered with "Service temporarily overloaded" often enough that A0,
  A2 and A4 served 20 turns on their fallback route. During A4's second pass, Switchyard's model calls failed
  with HTTP 404 for nine minutes.
- **Fixes during the run.** The receipt fix reached the VM between the two packs, and the news-universe,
  `market_scan` window and relationship-graph fixes only after the run ([after the tool
  fixes](#after-the-tool-fixes)).

## The earlier bake-off (2026-09-29)

The first bake-off ran on the retired `market-analysis` pack: its seven hero questions (H1 to H7) and five
more, once per arm, on the same kind of gateway for every model, with the stack on a laptop and Auto
Ontology off. Sol pinned passed 11 of 12; Ultra alone and Ultra → Sol 6 each (the old judge latched falsely on
half of Ultra's passes); Ultra → Sol with two confirmations 6; and Super → Ultra 3. Those results set today's defaults, and the 2026-09-30 bake-off confirms them.

## Repeating the bake-off

Repeat it when a model id, a template, the judge prompt or the endpoint's model set changes.

1. `./scripts/demo.sh up` with the profiles to measure, then `./scripts/demo.sh check`.
2. For each arm: set the arm's lines in `.env` (table above), run `./scripts/demo.sh restart switchyard`, and
   confirm the rendered line in `./scripts/demo.sh logs switchyard` (`switchyard: <template> on <endpoint> …`).
3. Submit each question one at a time through the API (`POST /v1/jobs/async/submit`, with the question's
   `sources` from `questions.yaml` as `data_sources`), wait for it to finish, and save
   `GET /v1/jobs/async/job/{id}/export` and `GET 127.0.0.1:4000/v1/routing/session-stats?session_id={id}`.
4. Run every arm twice, the second pass in reverse arm order.
5. Score each run with a deterministic check against the built database and an LLM judge, and fill in the tables.
6. Put `.env` back on the winner and restart Switchyard.

The bake-off took about 8 hours, one run at a time; the Sol arms' stalls and build.nvidia.com's overloads took
a large share of it.
