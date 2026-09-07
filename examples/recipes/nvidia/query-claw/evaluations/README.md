# Query Claw evaluations

Use `smoke.json` for a small public qualification of all Query Claw routes.
Its six deterministic cases remain the deployment gate. Use
`scenarios.json` for source constraints, a two-turn source switch, capability
discovery, unsupported-source abstention, and report formatting.
These suites target the stock NemoClaw v0.0.120 stack: Hermes 0.20.6,
OpenShell 0.0.106, and NemoClaw's native managed-MCP lifecycle.
Keep proprietary questions, source data, expected facts, and detailed answers
outside this repository in an owner-readable JSON or JSONL suite.

## Run a suite

Run the evaluator on the host where Hermes is forwarded to loopback:

```bash
API_SERVER_KEY="$(nemohermes "${NEMOCLAW_SANDBOX_NAME:-query-claw}" gateway-token --quiet)" \
  python3 scripts/evaluate_live.py \
    --suite /private/path/development.jsonl \
    --output ".runtime/evaluations/development-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
```

When the deployment uses a nondefault API port, export
`NEMOCLAW_HERMES_API_PORT` with that loopback port before running the command.

Use `--case <id>` to select a case and `--fail-fast` only for service diagnosis.
Select multi-turn cases in suite order including their earlier turns. By
default, independent cases continue after a failure; later turns in a failed
conversation are skipped and its temporary session is deleted.

Run the public behavioral scenarios separately:

```bash
API_SERVER_KEY="$(nemohermes "${NEMOCLAW_SANDBOX_NAME:-query-claw}" gateway-token --quiet)" \
  python3 scripts/evaluate_live.py --suite evaluations/scenarios.json
```

Each input line has this shape:

```json
{
  "id": "document-policy",
  "session": "optional-conversation-name",
  "prompt": "What policy applies to this request?",
  "expected": {
    "routes": ["retriever"],
    "tools": ["retriever.query"],
    "tool_order": ["retriever.query"],
    "facts": ["expected deterministic fact"],
    "citations": ["expected source locator"],
    "abstain": false
  }
}
```

Consecutive cases with the same optional `session` value share one temporary
Hermes conversation. The evaluator rejects a session name reused after another
session or standalone case, and deletes the temporary conversation after its
last turn. Omit `session` to isolate a case as before.

Allowed routes are `ontology`, `retriever`, and `kumo`. The `facts` and
`citations` fields are case-insensitive literal-presence checks, not semantic
truth checks; use them only for stable identifiers and deterministic values.
The successful query and calculation tool set must equal `expected.tools`
exactly: a missing or extra route query tool fails closed. Hermes may
call the read-only `skills_list` and `skill_view` tools to load native Query
Claw skills; the evaluator retains those calls in its receipt but excludes them
from the exact query-tool comparison. `skill_manage` and every unrelated
builtin remain disallowed.
Set `allow_calculation` to `true` and include `execute_code` in `tools` only for
a bounded calculation case. The evaluator inspects the requested Python, allows
only numeric-scalar arithmetic, a short safe-function list, and exactly one
printed numeric result, then grants exactly one non-persistent `once` approval.
This AST restriction is an evaluator and agent contract, not an OpenShell
policy. A dashboard operator can receive a broader `execute_code` request and
must inspect and deny anything outside the intended arithmetic. OpenShell policy
and the endpoint-side query contracts remain the enforcement boundary. A
second, persistent, unsafe, or unrelated approval fails the evaluator, as does
every unrelated builtin.
Expected facts and citations are never sent to Hermes.

Set `expected.format` to `markdown_report` only when the answer must contain
`Findings`, `Evidence`, and `Uncertainty` sections.

The evaluator refuses to overwrite an existing receipt. The mode-`0600` output
contains case ID, pass/fail, latency, tool names, and a bounded failure reason.
It intentionally excludes prompts, answers, expected facts, evidence, tool
payloads, provider usage payloads, and evaluator gold. A shared receipt ID, UTC
start time, suite SHA-256, and evaluator version group records from one
invocation without exposing suite contents. `declared_stack_versions` records
the recipe pins, and `declared_model` records the optional model value from the
evaluator environment; neither field is a live attestation of the running
sandbox. Judge runs also record the model actually requested from the judge and
the rubric version. Each route query tool may make one initial call and at most
one corrective retry; a third call fails the case.
The `tools` list still preserves execution order and repeated calls in failed
receipts so loops remain visible during diagnosis.

This evaluator is a route-qualification and deterministic-evidence gate. Add an
optional OpenAI-compatible semantic judge whose endpoint supports JSON Schema
structured output with:

```bash
# Set these three variables in .runtime/deploy.env, then export their values in
# this trusted shell without printing them.
export QUERY_CLAW_JUDGE_BASE_URL="https://judge.example.com/v1"
export QUERY_CLAW_JUDGE_MODEL="judge-model"
export QUERY_CLAW_JUDGE_API_KEY="replace-me"
API_SERVER_KEY="$(nemohermes "${NEMOCLAW_SANDBOX_NAME:-query-claw}" gateway-token --quiet)" \
  python3 scripts/evaluate_live.py \
  --suite evaluations/scenarios.json \
  --judge \
  --output .runtime/evaluations/scenarios.jsonl
```

The judge scores groundedness, completeness, evidence-type separation, and
uncertainty/format from 0–2. A zero fails its dimension, and any material
hallucination is a hard failure. Only the numeric scores and hallucination
boolean enter the receipt. It receives the question, answer, and minimum
expected checks—but never raw tool payloads—so use only a judge provider
approved for the evaluated data. The checks are anchors rather than an
exhaustive source transcript: deterministic checks establish the exact public
facts and route, while the judge assesses their presentation. It cannot
independently validate additional answer details. None of that content is sent
to Hermes as gold.

The judge improves semantic coverage but does not prove correctness by itself.
Likewise, an abstention pass only proves that the answer contains an explicit
abstention; it does not prove that the answer disclosed no unsupported content.

## Improve without overfitting

1. Keep a small development set and a separate held-out set. Do not expose the
   held-out questions or expected answers to the skill.
2. Establish a baseline, then classify failures as service/infrastructure,
   routing, evidence retrieval, answer grounding, evaluator, or unsupported
   data.
3. Change a skill only for a reusable decision rule. Put source-specific
   schemas and prediction targets in the read-only adapter metadata, not in
   agent instructions.
4. Rerun the development set, then the unchanged held-out set. A gain that does
   not transfer is not an improvement.
5. Report route accuracy, grounded deterministic checks, abstention, latency,
   tool failures, and semantic answer quality separately instead of relying on
   the evaluator's single smoke pass rate.

A private benchmark matrix can be used as an external overlay through this same
format, but only cases backed by the data sources deployed to Query Claw are
answer-quality tests. Cases for absent databases or corpora should be excluded
or explicitly evaluated as abstentions. Do not copy private prompts, corpora,
receipts, or expected values into this public recipe.
