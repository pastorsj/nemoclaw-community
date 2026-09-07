---
name: query-claw-structured
description: Answer exact supply-chain record questions with NVIDIA Ontology. Use for entity IDs, statuses, filters, counts, sums, and groupings; do not use for document text, policies, forecasts, or risk scores.
---

# Query Claw structured records

Use this skill for the **records** view of the Query Claw supply-chain data
product. If the operator selected records only, never call Retriever or Kumo.

1. For a clear question, call
   `mcp__query_claw__ask_question` with the complete structured portion of the
   operator's wording; never summarize away a field, value, date, population,
   ranking, or other records filter. Do not include requests assigned to the
   documents or predictions views. When
   the operator positively limits records to a named supplier, facility, or
   product, also pass each canonical display name in `entity_filters`. Leave
   `entity_filters` empty for comparisons, exclusions, and names mentioned
   only as context.
2. If terminology is ambiguous, call
   `mcp__query_claw__search_terms` with a short query and bounded
   limit, then ask the clarified question.
3. Use `mcp__query_claw__check_answerable` only when coverage is
   uncertain. Use `mcp__query_claw__check_readiness` only to diagnose
   a service error.
4. Treat returned rows as observed evidence at the source timestamp. Preserve
   entity or query IDs, row count, and truncation. Narrow a truncated query
   before claiming completeness. Ask Ontology only for domain fields and their
   original identifier column names; the adapter supplies row-count and
   truncation metadata, so do not ask SQL to synthesize them.

Do not infer policy language, a future outcome, or causation from records. If a
required entity is absent, say the question is unsupported rather than
guessing. End with:

```text
Sources used: records (NVIDIA Ontology)
```
