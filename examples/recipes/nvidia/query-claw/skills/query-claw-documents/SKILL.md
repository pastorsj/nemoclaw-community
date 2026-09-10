---
name: query-claw-documents
description: Retrieve cited document evidence from a selected Query Claw dataset with NeMo Retriever. Use for notices, policies, contracts, manuals, and quoted text; do not use for database totals, entity status, forecasts, or risk scores.
---

# Query Claw documents

Use this skill for the **documents** view of the selected Query Claw dataset. If
the operator selected documents only, never call Ontology or predictions.

Call `mcp__query_claw__query` with a focused question, the exact selected
`dataset_id`, the unchanged `scope_token` when supplied, and `top_k` no greater
than 5. The tool always returns citation-ready evidence. Cite the returned
document and locator for each factual claim. Quote only the words needed to
support the answer; do not compare scores from unrelated queries as if they
share one scale. Never expose a scope token.

Empty results, weak coverage, or missing locators mean the document claim is
unsupported. State the gap and ask for a narrower question or better indexed
source. Never fill it from memory, records, predictions, or web search. End
with:

```text
Sources used: documents (NeMo Retriever)
```
