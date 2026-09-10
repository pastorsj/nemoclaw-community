---
name: query-claw-structured
description: Answer exact record questions in a selected Query Claw dataset with NVIDIA Ontology. Use for entity IDs, statuses, filters, counts, sums, and groupings; do not use for document text, policies, forecasts, or risk scores.
---

# Query Claw structured records

Use this skill for the **records** view of the selected Query Claw dataset. If
the operator selected records only, never call Retriever or predictions.

1. For a clear question, call
   `mcp__query_claw__ask_question` with the complete structured portion of the
   operator's wording; never summarize away a field, value, date, population,
   ranking, or other records filter. Do not include requests assigned to the
   documents or predictions views. Pass the exact selected `dataset_id` and,
   when supplied, the unchanged `scope_token`.
2. If terminology or dataset selection is ambiguous, ask the operator to
   clarify before querying.
3. Use `mcp__query_claw__check_answerable` only when coverage is uncertain,
   with the same `dataset_id` and optional `scope_token`. Use
   `mcp__query_claw__check_readiness` only for initial discovery or to diagnose
   a service error. Coverage is not answer evidence: after a positive coverage
   result, call `mcp__query_claw__ask_question` before answering.
4. Treat returned rows as observed evidence at the source timestamp. Preserve
   entity or query IDs, row count, and truncation. Narrow a truncated query
   before claiming completeness. Ask Ontology only for domain fields and their
   original identifier column names; the adapter supplies row-count and
   truncation metadata, so do not ask SQL to synthesize them.

Do not infer policy language, a future outcome, or causation from records. Never
expose a scope token. If a required entity is absent, say the question is
unsupported rather than guessing. End with:

```text
Sources used: records (NVIDIA Ontology)
```
