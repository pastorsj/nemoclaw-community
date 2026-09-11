---
name: query-claw-structured
description: Answer exact record questions against Query Claw's structured data product through the official NVIDIA GSF MCP server. Use for entity IDs, statuses, filters, counts, sums, and groupings; do not use for document text, policies, forecasts, or risk scores.
---

# Query Claw structured records

Use this skill for observed records in the one structured data product connected
to this GSF deployment. Official GSF tools do not accept `dataset_id`,
`scope_token`, or `target_db`; never invent or pass them. The active document
corpus does not change the GSF data product.

1. Call `mcp__gsf__ask_question` once with the complete structured portion of
   the operator's wording. Preserve every field, value, date, population,
   ranking, and records filter. Do not include document or predictive requests.
2. Treat returned rows as observed evidence. Preserve the returned SQL, entity
   IDs beside their labels, row count, and truncation state. Retain the component
   measures behind every derived total or rate. Narrow a truncated question
   before claiming completeness.
3. Preserve every requested grouping dimension in the result. When joining a
   parent measure to one-to-many child rows, aggregate the parent at its natural
   entity grain before rolling it up; never multiply an amount through join
   fanout.
4. For a timeline that genuinely mixes grains, ask a separate complete
   structured subquestion for each grain, then align results only on explicit
   time boundaries. Never collapse daily, weekly, monthly, or event-level rows
   into an unlabeled common grain.

Do not infer policy language, a future outcome, or causation from records. If a
required entity or relationship path is absent, return the supported dimensions
and state the limitation rather than guessing.
When this skill is one leg of a multi-source request, let the coordinator write
the single combined source footer instead of emitting a separate footer here.
End with:

```text
Sources used: records (NVIDIA GSF)
```
