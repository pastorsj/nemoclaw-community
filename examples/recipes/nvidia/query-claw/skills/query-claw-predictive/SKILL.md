---
name: query-claw-predictive
description: Run governed Kumo-backed predictions for a selected Query Claw dataset through NVIDIA Ontology. Use for future outcomes, probabilities, forecasts, and risk rankings; do not use for observed facts, document claims, or causal conclusions.
---

# Query Claw predictions

Use this skill for the **predictions** view of the selected Query Claw dataset.
Predictions are routed through NVIDIA Ontology and its governed Kumo
integration. Never call Kumo directly or construct PQL.

For a question whose requested result is itself a forecast, probability, risk
ranking, or model attribution, use predictions alone unless the operator
explicitly asks to combine that result with observed records or documents.

Before calling a tool, require a clear outcome or target, entity or population,
and prediction horizon or endpoint. If any of those is ambiguous or missing,
ask one concise clarifying question and do not call `predict` yet.

Call `mcp__query_claw__predict` once with the complete prediction question, the
exact selected `dataset_id`, and the unchanged `scope_token` when supplied. Do
not send observed-record or document requests to this route. If the selected
dataset has no predictions view, abstain without trying another dataset.

Report the target, anchor time, horizon, population, and returned score or
probability when the tool provides them. Label each result **Predicted**. Use
only returned assumptions, warnings, or graph receipt to qualify a prediction;
never invent an explanation. Attributions describe influence, not causation.
Never use evaluation labels or future outcomes that were unavailable at the
anchor time, and never expose a scope token. If the result does not support the
request, abstain. End with:

```text
Sources used: predictions (NVIDIA Ontology / Kumo)
```
