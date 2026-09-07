---
name: query-claw-predictive
description: Run bounded Kumo predictions for the Query Claw supply-chain graph. Use for future outcomes, probabilities, forecasts, and risk rankings; do not use for observed facts, document claims, or causal conclusions.
---

# Query Claw predictions

Use this skill for the **predictions** view of the Query Claw supply-chain data
product. If the operator selected predictions only, never call Ontology or
Retriever.

For a question whose requested result is itself a forecast, probability, risk
ranking, or model attribution, use predictions alone unless the operator
explicitly asks to combine that result with observed records or documents.

1. Call `mcp__query_claw__inspect_graph_metadata` once. Confirm that its
   prediction contract matches the requested cutoff, target, and horizon.
2. Pass the returned contract PQL and prediction-population entity IDs to
   `mcp__query_claw__predict` once. The adapter fixes the manifest cutoff
   and 30-day horizon; keep that service-owned population and execution bound.
3. Call `mcp__query_claw__explain` only when the operator explicitly asks why
   an entity ranked highly or requests prediction attribution. Call it once for
   only the highest-risk returned entity unless the operator asks for
   additional entities.

Report the target, anchor time, horizon, population, and returned score or
probability. Label each result **Predicted**. Attributions describe influence,
not causation. Never use evaluation labels or future outcomes that were
unavailable at the anchor time. If metadata does not support the request,
abstain. End with:

```text
Sources used: predictions (Kumo)
```
