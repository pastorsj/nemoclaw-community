<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# execution-receipts

A Hermes plugin that keeps each job's data tools inside the job's selected sources and records what
every data-tool call returned. The job API turns the receipts into the execution graph and the
evidence the answer cites. The image bakes it into `/opt/data/plugins/`, and `profile/config.yaml`
enables it (`plugins.enabled: [execution-receipts]`).

The job API starts each run with the job id as the Hermes `session_id`, so the plugin reads the job id
from there. It knows the data tools from the tool registry, baked at
`/opt/agent/contracts/tool-registry.json`.

## Hooks

| Hook | What it does |
| --- | --- |
| `pre_tool_call` | Blocks `skill_manage` (defense in depth; `skills.write_approval` already stages writes) and any `mcp__*` tool that is not in the registry. For a registered tool, reads the job's execution scope once per job and sets `source_ids` to the selected sources whose `capabilities` include the tool's `family`: retrieval gets the document sources, the market tools the structured source. `ask_question` needs a selected source that allows `structured_retrieval` but gets no arguments, because Auto Ontology serves only the pack's database; its receipt takes `databaseName` from the scope. The hook also removes any `conversation_id`, `target_db`, `prediction`, `evidence` or `source_ids` the model passed to `ask_question`, from the argument dict Hermes then dispatches (a `modify` directive can only add keys). When the scope cannot be read, or no selected source allows the tool, the call is blocked. |
| `post_tool_call`, `transform_tool_result` | Whichever fires first (the agent loop fires `transform_tool_result` first; a blocked or raised call gets only `post_tool_call`) builds a `ReceiptV2` (`contracts/schemas/receipt.schema.json`) from the tool's structured result, fits it to the display limits and posts it. `artifactKind` is the tool's registry `receipt_kind`; the mapping and the fitting rules are in [contracts/README.md](../../../../contracts/README.md#from-tool-result-to-receipt). Best effort: a failure is logged and never fails the tool call. |
| `transform_tool_result` | Also puts `evidence_id` (the `receiptId`) first in the result the agent sees, but only when the job API stored a completed receipt. `SOUL.md` tells the agent to cite it. A data tool's result longer than `MAX_RESULT_CHARS` (30,000) as the agent reads it is shortened: rows from the end of its longest list, then its longest text, with `shortened_to_fit` saying what was left out ([tool result size](../../../../docs/architecture.md#tool-result-size)). |
| `post_api_request` | Reports each model call: the model Switchyard served, its tier and the token counts. |

Receipts use Hermes' own `duration_ms` for the call, and the plugin's clock for `occurredAt`.
Hermes gives plugins no trace context, so `traceId` and `spanId` are null.

## Job API

The base URL is `HERMES_RECEIPT_API_URL` (image: `http://host.openshell.internal:8000`). Every request
sends `X-Receipt-Key: $HERMES_RECEIPT_API_KEY`, an OpenShell placeholder that the sandbox swaps for
the real key on these routes only. Each call times out after 5 seconds.

| Route | Body |
| --- | --- |
| `GET /internal/hermes/jobs/{job_id}/execution-scope` | Returns `{job_id, sources: [{id, capabilities}], database_name, collection, models: {efficient, capable}}`. `capabilities` are registry `family` values. |
| `POST /internal/hermes/jobs/{job_id}/tool-receipts` | One `ReceiptV2`, camelCase, every key present. |
| `POST /internal/hermes/jobs/{job_id}/llm-calls` | `{api_request_id, turn_id, served_model, tier, input_tokens, output_tokens, started_at, completed_at}`. `tier` is `efficient` or `capable` when the served model matches the scope's `models`, else null. `input_tokens` is the whole prompt, cached tokens included. The job API emits it as an `llm.call` event. |
