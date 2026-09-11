---
name: query-claw-predictive
description: Ask predictive questions against Query Claw's structured data product through the official NVIDIA GSF MCP server. Use for future outcomes, probabilities, forecasts, and risk rankings; do not use for observed facts, document claims, or causal conclusions.
---

# Query Claw predictions

GSF may route a clearly predictive `ask_question` request through its configured
Kumo integration. The official MCP server deliberately exposes no separate
`predict` tool and no way to force that internal route. Never call Kumo
directly, construct PQL, or claim Kumo was used without evidence returned by
GSF.

1. Require a clear outcome or target, entity or population, and explicit
   forecast horizon or cutoff. Ask one concise clarifying question if any is
   missing.
2. Call `mcp__gsf__ask_question` once with the complete predictive wording.
   Official GSF tools do not accept `dataset_id`, `scope_token`, or `target_db`.
   Treat a returned service or PQL error as a completed attempt; do not repeat
   the same prediction through this tool. If GSF instead returns ordinary SQL,
   make at most one clearer retry that explicitly names the target, population,
   and horizon, then report the predictive route as unconfirmed if it remains
   ordinary SQL.
3. A returned `sql` beginning with `PREDICT` proves GSF attempted its Kumo
   route. Label each result **Predicted** and call it Kumo-backed only when every
   returned prediction row also contains a finite numeric score. If the route
   was attempted but returned an error, no rows, or no usable scores, say the
   Kumo prediction was attempted but unavailable. Otherwise say GSF answered
   but the predictive route is unconfirmed.
4. If the original request also needs observed facts or calculations, follow
   `query-claw-structured` once for those independent inputs even when the
   prediction attempt fails. Also follow `retriever-mcp` when it independently
   needs document evidence. Return a scoped partial answer without replacing a
   missing prediction with historical values.

Report the returned target, anchor time, horizon, population, score or
probability, and material warnings when present. Never invent a model
attribution or explanation. Attributions describe influence, not causation. Do
not use evaluation labels or future outcomes unavailable at the anchor time. If
the result does not support the request, abstain.

When this skill is one leg of a multi-source request, let the coordinator write
the single combined source footer instead of emitting a separate footer here.
When the returned result proves a successful Kumo prediction, end with:

```text
Sources used: predictions (NVIDIA GSF / Kumo)
```

When `PREDICT` proves an attempt but no usable prediction scores were returned,
end with:

```text
Sources used: prediction attempt (NVIDIA GSF / Kumo; unavailable)
```

Otherwise end with:

```text
Sources used: predictive answer (NVIDIA GSF; route unconfirmed)
```
