<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Execution feature

What the agent did for an answer: a graph of the run (one fixed topology of everything a Hermes
run can use, lit by what this run used), an explorer per tool call built from its receipt, a data
viewer, a Phoenix link and step-by-step replay, plus the Agent Activity panel (Thinking, Timeline
and Benchmark). It plugs into the base UI through the one `ExecutionFeature` slot
(`@/shared/context`), wired in `app/providers.tsx`.

## Data flow

```text
live:   SSE `execution.v2`, `job.status` ───┐
        GET /v1/jobs/async/job/{id}/export ─┤ (receipts, benchmark, and the events of an earlier run)
replay: /api/recordings/sessions/<id>.json ─┘
                                            ▼
      store.ts (runs by job id: events, receipts, job status, benchmark; nothing persisted)
                                            ▼
     projection.ts (events up to the replay cursor → tool calls, model calls, status)
          ▼                      ▼                                   ▼
 graph/graph-events.ts  activity/activity-model.ts          receipt-summary.ts, explorers/*
 graph/graph-model.ts    (Thinking items, Timeline)                  ▼
          ▼                      ▼                          explorers/*Inspector.tsx
 graph/ExecutionGraph.tsx activity/ActivityPanel.tsx ── benchmark/BenchmarkTab.tsx
          ▼
 ExecutionWorkspace.tsx
```

The types are generated from the API's models (`@/generated`, never edited by hand) and every
tool name, label and explorer comes from the tool registry (`TOOL_REGISTRY`). The view-models are
pure functions. Everything takes the original demo UI's look: the workspace, replay bar, graph,
explorers and data viewer from `execution-workspace.module.css` and the explorers' CSS modules, the
Agent Activity panel from `activity/execution-timeline.module.css` and the Benchmark tab from
`benchmark/*.module.css`.

## Files

| Path                        | Role                                                                                                                                                                        |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contract.ts`               | Shape guard for events, receipts and benchmarks; anything else is dropped and counted                                                                                       |
| `registry.ts`               | Tool lookup by MCP or Hermes name; the resource behind each capability family; the logos on each tool node                                                                  |
| `store.ts`, `projection.ts` | Run state, and its reduction to what the views show                                                                                                                         |
| `graph/`                    | The execution graph: events as graph facts, the fixed topology and its states, GPU badges, the renderer                                                                     |
| `explorers/`                | Per node: market tools (`MarketToolExplorer`, `MarketToolInspector`), Auto Ontology (`OntologyLineageInspector`), the rest (`EvidenceInspector`, from `receipt-summary.ts`) |
| `activity/`                 | The Agent Activity panel's tabs: Thinking (display-safe milestones) and Timeline (the finished run's spans, with each action's recorded result)                             |
| `benchmark/`                | The Benchmark tab: the run's tool calls, each market call's CPU and NVIDIA GPU medians, and the speedup only when the API qualified it                                      |
| `replay/`                   | Recordings bundle and job export (`sources.ts`), the replay cursor and its controls                                                                                         |
| `data-viewer/`              | The Structured Database browser: tables, previews and read-only SQL; in replay, the bundle's copy (`database.json`)                                                         |
| `trace-link.ts`             | Phoenix trace and span links (shown when `PHOENIX_URL` is set)                                                                                                              |

The panel follows the running job, else the last answer of the open conversation, so a reopened
session, recorded or saved, shows its activity (`selectActivityJobId` in `features/chat`).

## API it reads

- `GET /v1/jobs/async/job/{id}/export`: one turn, `{jobId, question, …, events, receipts, benchmark}`.
- `GET /v1/jobs/async/job/{id}/trace`: `{trace_id}`, linked as `<PHOENIX_URL>/redirects/traces/<id>`.
- `GET /v1/jobs/async/job/{id}/stream`: the `execution.v2` events, and `job.status`, whose final
  status (`success`, `failure`, `interrupted`) decides how the run ended.
- `GET` and `POST /v1/jobs/async/job/{id}/benchmark`: the stored CPU/GPU comparison, and running one
  (`api/README.md#benchmark`).
- `GET /v1/data_sources/{id}/schema`, `…/preview?table=&limit=` and `POST …/query {sql}`.
- Replay mode reads only `/api/recordings/index.json`, `/api/recordings/sessions/<id>.json` and
  `/api/recordings/database.json` (bundle v2, written by `scripts/demo.sh record`) and never calls the
  API. The data viewer then shows the copied schema and rows, and reruns only the queries the
  recording ran; the Benchmark tab shows the recorded comparison, if any.

## Adding a tool

Add it to `contracts/tool-registry.json` and regenerate. A tool with an existing `receipt_kind`
needs no UI change: the graph draws it under its capability's nodes, or as Other Tools until it
gets a node of its own in `graph/graph-model.ts` (`hermesExecutionGraphNodes`, the edges into and
out of it, and `TOOL_NODE_BY_NAME`). A new kind adds a receipt model in the API, a `case` in `receipt-summary.ts`
and an entry in `ARTIFACT_KINDS` (`contract.ts`), which TypeScript then requires. To draw the logo of a library the tool is built on, as Retrieve Evidence draws
LangChain's, add the file to `public/ecosystem-logos` and an entry to `TOOL_LOGOS` (`registry.ts`).
