# Contracts

The JSON that services share. Services never import each other's code; they agree on these files.

| Path | Source | Used by |
| --- | --- | --- |
| `tool-registry.json` | Hand-written | api, agent plugin, UI (as `TOOL_REGISTRY`) |
| `tool-registry.schema.json` | Hand-written | Validates the registry |
| `schemas/execution-event.schema.json` | Generated from `api/src/demo_api/events/` | UI types, replay bundles |
| `schemas/receipt.schema.json` | Generated from `api/src/demo_api/receipts/` | UI types |
| `schemas/benchmark.schema.json` | Generated from `api/src/demo_api/benchmark/` | UI types (the Benchmark tab), replay bundles |
| `schemas/retrieval-benchmark.schema.json` | Generated from `api/src/demo_api/benchmark/retrieval.py` | `retrieval-benchmark` (`tools/retrieval`), UI types (the Benchmark tab's Milvus comparison), replay bundles |
| `schemas/pack.schema.json` | Generated from `api/src/demo_api/pack.py` (`PackView`, `GET /v1/pack`) | UI types (the landing page and the composer's example picker) |

TypeScript for the schemas and the registry is generated into `ui/src/generated/`.

## Regenerate

```bash
scripts/gen-contracts.sh           # after changing a model or the registry
```

The script exports the Pydantic models as JSON Schema, then runs `json-schema-to-typescript` and Prettier (both pinned). Commit
what it writes. Never edit `schemas/` or `ui/src/generated/` by hand.

## Tool registry

One entry per MCP tool. Adding a tool starts here.

| Field | Meaning |
| --- | --- |
| `id` | MCP tool name, e.g. `retrieve_evidence` |
| `server` | Hermes MCP server key: `retrieval`, `market_analytics`, `auto_ontology` |
| `hermes_name` | `mcp__<server>__<id>`, the name Hermes and the receipts use |
| `family` | Capability a data source must grant: `unstructured_retrieval`, `market_analytics`, `structured_retrieval`, `structured_prediction` |
| `label`, `description` | Display text for the UI |
| `explorer` | UI explorer: `retrieval`, `market`, `sql`, `pql`, `ontology` |
| `receipt_kind` | The `artifactKind` of the tool's receipts |
| `pills` | The technology pills the UI shows for a run that used the tool (`Pill`: `cudf`, `cuml`, `cugraph`, `kumo`, `retrieval`, `ontology`, in display order). Every market tool has `cudf`; `market_anomaly_scan` adds `cuml` and `analyze_market_relationships` `cugraph`. A pack's `questions.yaml` `tools` uses the same names |
| `feature` | The agent feature (`AGENT_FEATURES`) that provides the tool: `retrieval`, `analytics` and `ontology` come with the Compose profiles of those names, `kumo` with a Kumo service's URL and key ([Kumo service](../docs/kumo-service.md)) |

## Execution events (`execution.v2`)

The API emits every execution observation as one `ExecutionEventV2`. The event store gives each
row a monotonic per-job cursor. `GET /v1/jobs/async/job/{job_id}/stream` sends each event as an
SSE frame with `event: execution.v2`, `id: <cursor>` and the event (without `cursor`) as data.
Clients resume with `/v1/jobs/async/job/{job_id}/stream/{cursor}` or `Last-Event-ID`.

The `tool.*` and `artifact.*` events of a registered tool carry the tool's registry `family` as
`capabilityId`, and the family decides `componentId` (`COMPONENT_BY_FAMILY` in
`api/src/demo_api/events/execution.py`):

| `family` | `componentId` |
| --- | --- |
| `unstructured_retrieval` | `milvus.retrieval` |
| `market_analytics` | `nvidia.market_analytics` |
| `structured_retrieval` | `nvidia.ontology` |
| `structured_prediction` | `nvidia.kumo` |

`display.attributes` is open JSON with snake_case keys. Model calls are `llm.call` events whose
attributes carry `served_model` (the model Switchyard served) and `tier` (`efficient` or
`capable`), so the UI can show when a run escalates. Token counts are `input_tokens` and
`output_tokens`; `prompt_tokens` and `completion_tokens` are rejected (see
[the limits](#display-safe-json-limits)).

When a run succeeds, the API records its publication as three last events, in this order, as the original demo
did:

| `eventKind` | Label | `display.attributes` |
| --- | --- | --- |
| `report.completed` | Response formatted | – |
| `report.reference_resolution` | Citations resolved | `status` (`reference_ids_resolved`, `partial`, `evidence_uncited` or `no_evidence`), `total_citations`, `uncited_evidence_count`, `invalid_evidence_count` |
| `report.metrics` | Run metrics available | `runtime_profile` (the model the run asked Hermes for), `wall_duration_ms`, `tool_call_count`, `known_tool_duration_ms`, token counts |

## Receipts (`ReceiptV2`)

The agent plugin posts one receipt per registered tool call to
`POST /internal/hermes/jobs/{job_id}/tool-receipts`. `receiptId` is the evidence id the agent
cites; events reference it in `artifactRefs`. The union is discriminated by `artifactKind`,
one variant per `receipt_kind` in the registry. Content mirrors the tool's own result:

| `artifactKind` | Tools | Content |
| --- | --- | --- |
| `retrieval_evidence` | `retrieve_evidence` | The tool's `RetrievalResult` |
| `analytics_result` | the seven market tools | The tool's `MarketResult`, plus `public_parameters` |
| `structured_query` | `ask_question` | Question, SQL, rows, ontology lineage |
| `structured_prediction` | `predict_asset_outcomes` | The tool's `PredictionResult` |

The API accepts field names in snake_case or camelCase and serves camelCase. Keys inside open
JSON (`payload`, `publicParameters`, `rows`, `metadata`) are kept as sent.

A completed receipt always has content. A failed one may carry `errorType`, `errorSummary` and
any bounded content the tool returned.

### From tool result to receipt

The plugin never makes up a value. When a tool does not report a fact, the receipt does not
carry it.

| Receipt field | Source |
| --- | --- |
| `toolName` | The registry `hermes_name` |
| `durationMs` | Hermes' `post_tool_call` `duration_ms`: the tool's own execution time |
| `occurredAt` | The plugin's clock at `post_tool_call` |
| `status` | `failed` when the call raised or the result reports a failure (analytics `status: failed`, prediction `available: false`); otherwise `completed` |
| `errorType` | The analytics `error.code`, `evidence_unavailable` for an unavailable prediction, or the Hermes error category when the call raised |
| `errorSummary` | The analytics `error.message`, the prediction `reason`, or the Hermes error |
| `content` | The tool's result, fitted to the limits below |

Per tool, `content` is:

- `retrieve_evidence`: the result as returned. A `published_at` that is not a timestamp with a
  timezone becomes null.
- The market tools: the result as returned, plus `public_parameters`, the call's arguments
  without `source_ids`.
- `predict_asset_outcomes`: the result as returned.
- `ask_question`: the question, the target database, Auto Ontology's answer and SQL, up to 25
  rows (`truncated` and `source_row_count` record any cut) and the resolution lineage.

Compared with the prototype's receipts, retrieval, analytics and prediction content now uses the
current tools' field names. The synthetic `receipt_tool_name`, the locator and content digest
(both derivable from `receiptId`), the derived counts and statuses, and the GPU-index, benchmark,
cache and scope-grant fields are gone.

## Display-safe JSON limits

The API runs every receipt `content` and every event's `display.attributes` through
`validate_bounded_display_json` in `api/src/demo_api/events/models.py`. JSON Schema cannot
express these rules, so `receipt.schema.json` does not show them. One violation rejects the whole
receipt or event.

| Rule | Limit |
| --- | --- |
| `MAX_JSON_DEPTH` | Values sit at most 6 levels below `content` or `attributes` |
| `MAX_JSON_ITEMS` | 100 items per array, 100 fields per object |
| `MAX_JSON_NODES` | 2,000 nodes in total, counting objects, arrays and values |
| `MAX_JSON_TEXT_CHARS` | 32,000 characters per string |
| Keys | 1 to 128 characters |
| Numbers | Finite |
| Text | No control characters except tab, line feed and carriage return |

An object key is rejected, at any depth, when this case-insensitive pattern matches anywhere in it:

```text
thought|reasoning|prompt|embedding|password|passwd|secret|token|credential|api[_-]?key|
authorization|cookie|connection(?:_string)?|private[_-]?key|access[_-]?key|
filesystem|file[_-]?path|storage[_-]?uri|dsn
```

So `system_prompt`, `token_count` and `prompt_tokens` are all rejected. The exceptions are the
usage keys `input_tokens`, `output_tokens`, `total_tokens`, `reasoning_tokens`,
`cached_input_tokens`, `cache_write_tokens`, `max_output_tokens` and `token_usage`, and
`query_embedding_ms` when it is a finite, non-negative number.

The plugin fits each result before it posts it, as the prototype's projection did:

1. Drop every key that matches the pattern, such as a SQL column named `token_count`.
2. Cut each list to the schema's `maxItems`, or to 100 where the schema has none. SQL rows keep
   at most 40 columns, so 25 rows fit in the 2,000-node budget.
3. Record every cut. A payload list with a sibling flag (`series_truncated`, `points_truncated`,
   `events_truncated`) sets it to true; SQL rows set `truncated` and keep `source_row_count`.
4. Cut each string to the schema's `maxLength`, for example a hit's `snippet` to 1,500 characters.
5. Replace each control character the Text rule refuses with a space.
