# Query Claw Evaluations

Query Claw evaluates the same industry datasets and questions as the reviewed
AIQ3 booth-demo portfolio. The source checkout, built data, compiled suites,
answers, judgments, and reports remain outside this public repository or under
ignored `.runtime/` paths. Checked-in code supplies only the compiler, runner,
judge, and report generator.

The live runner recognizes exactly three answer capabilities:

| Capability | Hermes answer tool | Required observation |
| --- | --- | --- |
| Unstructured retrieval | NeMo Retriever `query` | Reranked hits from the active collection with source-bearing document references. |
| Structured retrieval | NVIDIA Ontology/GSF `ask_question` | Returned SQL and rows for the active database, where `sql` does not start with `PREDICT`. |
| Structured prediction | NVIDIA Ontology/GSF `ask_question` | Returned `sql` starts with `PREDICT` and every returned row carries a numeric prediction score. A scoreless result records an attempted route, not a successful observation. |

GSF exposes structured retrieval and Kumo prediction through the same official
MCP tool. Skill discovery may prepare a call but does not count as answer
evidence. Query Claw has no second GSF adapter, direct Kumo tool, web search, or
general code execution.

## Evaluation Scope

The reviewed AIQ3 source inventory contains 281 standalone questions. This
workflow compiles the 273 questions bound to one industry dataset: one
canonical wording per task across nine industries and nine datasets. The eight
remaining questions are seven cross-industry tasks and one platform-behavior
task. They are deliberately outside these suites because they do not fit the
one-active-dataset execution contract.

The excluded cross-industry task IDs are `help_all_sources_selected`,
`supply_query_with_documents_selected`,
`all_sources_ai_factory_hybrid_no_leakage`,
`cross_database_open_order_comparison`,
`supply_prediction_and_factory_document`,
`supply_prediction_and_factory_document_clarification`, and
`all_three_evidence_paths_risk_brief`. The excluded platform task is
`ordinary_question_no_sources_selected`.

The compiler fails unless it finds exactly the expected nine datasets and 273
industry-bound cases. Its `index.json` records the selected source IDs,
profiles, cohorts, expected capabilities, evidence contracts, and the number
of authored wording variants. A final report from this workflow therefore
covers 273 of 281 standalone questions, not the eight portfolio-level tasks and
not every alternate wording.

## Prerequisites

- An approved checkout of the reviewed AIQ3 booth-demo source, including its
  built dataset outputs.
- A Query Claw host with the requirements in the root
  [README](../README.md#quickstart).
- An existing Kumo API endpoint supported by the pinned GSF integration, only
  for datasets that declare prediction.
- Optional access to an approved Responses-compatible model for semantic
  judging.

The compiled index records the source portfolio hash, source checkout commit
when Git can resolve it, selected wording policy, and exact scope totals.

`deploy/setup.sh` completes official GSF OAuth headlessly inside Hermes when
GSF is enabled. Keep every generated path private and never commit dataset
outputs or result files.

Run these commands from the Query Claw directory.

## 1. Compile The Portfolio

```bash
python3 evaluations/compile_aiq3_portfolio.py \
  --aiq3-root /private/path/to/aiq-3-booth-demo \
  --output-dir .runtime/evaluations/aiq3
```

This creates nine schema-v3 suite files and `index.json` without copying source
data into this repository.

## 2. Select And Deploy One Dataset

Set the source repository and one manifest in `.runtime/deploy.env`:

```dotenv
QUERY_CLAW_DATASET_REPOSITORY=/private/path/to/aiq-3-booth-demo
QUERY_CLAW_DATASET_MANIFEST=/private/path/to/aiq-3-booth-demo/industries/<industry>/datasets/<dataset>/dataset.json
```

Then run:

```bash
bash deploy/setup.sh
```

Setup atomically copies only that dataset's declared DuckDB, ontology,
prediction support files, and documents into
`.runtime/active-data/active-dataset.json`. It starts only the capabilities
that exist, imports the reviewed ontology, seeds database-scoped PQL examples,
mounts the reviewed graph into GSF, ingests one Retriever collection when
documents exist, and applies the matching Hermes profile. This one-dataset
activation is the evaluation's isolation boundary.

Setup also writes `.runtime/retriever-provenance.json` with mode `0600`. Keep
this receipt with private evaluation results: it records the observed service
and image identity, effective embedding and reranking model IDs, and official
parser and chunker defaults without retaining endpoints, credentials, file
names, or document content.

## 3. Run The Matching Industry Suite

Capture the local Hermes API bearer without printing it and run the suite whose
industry and dataset match the activation:

```bash
API_SERVER_KEY="$(nemohermes "${NEMOCLAW_SANDBOX_NAME:-query-claw}" gateway-token -q)" \
  python3 evaluations/run.py \
    --suite .runtime/evaluations/aiq3/aiq3-<industry>-<dataset>.json \
    --index .runtime/evaluations/aiq3/index.json \
    --active-dataset .runtime/active-data/active-dataset.json \
    --output .runtime/evaluations/results/<industry>.jsonl
```

Use `--case <case-id>` one or more times for focused diagnosis. The runner:

- binds the suite to the activated dataset before calling Hermes;
- uses a separate non-stored Responses request for each question;
- records the public answer, complete tool sequence, attempted and successfully
  observed capabilities, and non-sensitive tool counts/statuses; transient SQL,
  PQL, rows, entity IDs, and prediction scores are never retained;
- verifies required capabilities, explicitly allowed optional capabilities,
  and bounded AIQ evidence contracts; and
- continues after a case failure, then exits nonzero when any case failed.

Retriever cases require the active collection, `top_k=5`, `format="hits"`, and
reranking. Structured and predictive classification uses the returned GSF
`sql`, not answer wording. Results are written
atomically after each case with mode `0600`; an interrupted output retains
completed cases, although rerunning requires a new output path.

Repeat selection, deployment, and execution for each suite in `index.json`.
Never run two suites against one sandbox concurrently.

## 4. Judge Each Answer File

```bash
AIQ_JUDGE_RESPONSES_URL=https://approved.example/v1/responses \
AIQ_JUDGE_API_KEY=<secret> \
AIQ_JUDGE_MODEL=<model> \
  python3 evaluations/judge.py \
    --input .runtime/evaluations/results/<industry>.jsonl \
    --output .runtime/evaluations/judged/<industry>.jsonl
```

The judge applies AIQ3's `enterprise_response_usefulness.v3` rubric and records
`usable`, `degraded`, or `unusable`, with confidence and reason codes. It is
nonblocking: absent, incomplete, unavailable, or truncated judge input is
recorded explicitly while deterministic run results remain intact. The judge
receives the question, answer, and selected source IDs; use only a provider
approved for that data.

Set `AIQ_JUDGE_RECORD_DELAY_SECONDS` or pass `--record-delay-seconds` (0–300,
default 0) to pace provider requests between records.

Run `evaluations/judge.py` even when no judge is configured if a report is
needed. It records the unavailable status and stable rubric provenance for
each result.

The judge does not fall back between models automatically. If a configured
model fails, rerun every answer file with one approved fallback model; the
report rejects mixed judge provenance. Record both the failed primary and the
model that actually judged the run in the qualification notes. Deterministic
checks are independent of the judge.

## 5. Build The Complete Report

```bash
python3 evaluations/report.py \
  --input .runtime/evaluations/judged/*.jsonl \
  --index .runtime/evaluations/aiq3/index.json \
  --markdown .runtime/evaluations/query-claw-report.md \
  --json .runtime/evaluations/query-claw-report.json
```

The report fails closed unless every case in the compiled index is present.
For an infrastructure-blocked diagnostic only, add `--allow-partial`; both
outputs are then prominently labeled partial and enumerate the missing cases.

The report aggregates deterministic pass rate, judge coverage, semantic
usefulness, and end-to-end helpfulness overall and per industry/dataset. It
also retains every question, answer, selected source, expected and observed
capability, tool sequence, deterministic check, failure, and judgment. The
JSON and Markdown identify the source portfolio hash, source checkout commit
when available, wording selection, and compiled scope from `index.json`.
Markdown escapes model-authored content instead of executing it as markup.
Both outputs are written with mode `0600`.

## Local Checks

The evaluation pipeline can be checked without private data or credentials:

```bash
python3 -m unittest \
  tests.test_compile_aiq3_portfolio \
  tests.test_evaluation_pipeline
```

These tests cover portfolio compilation, dataset binding, native tool-flow
classification, bounded evidence checks, judge failure handling, and report
aggregation. They do not prove live service availability or answer quality.
