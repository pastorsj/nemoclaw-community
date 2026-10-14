---
name: predicting-with-kumo
description: Predicts future asset outcomes with Kumo Relational
license: Apache-2.0
compatibility: Requires the market_analytics MCP server with an NVIDIA Kumo Relational service (its URL and key)
metadata:
  author: NVIDIA
  version: "1.0"
  hermes:
    tags:
      - kumo
      - prediction
      - forecasting
      - pql
    related_skills:
      - analyzing-market-data
      - querying-auto-ontology
---

# Predicting with Kumo

`predict_asset_outcomes` runs one curated predictive query (PQL) template on
Kumo Relational, a relational foundation model, served by an NVIDIA Kumo
Relational NIM that the deployment reaches by its URL and key. For
each asset in scope it returns the probability of the template's outcome over
the template's horizon. You choose the template and the assets. You never write
PQL.

## When to Use

- The selected sources include the `structured_prediction` capability, and
- the question asks how likely a future outcome is for one or more assets, and
  one of the tool's templates describes that outcome.

Questions about what already happened belong to `analyzing-market-data` or
`querying-auto-ontology`.

## Tool

`predict_asset_outcomes` (Hermes tool ID
`mcp__market_analytics__predict_asset_outcomes`). The tool is registered only
when Kumo is configured. If it is not in your tool list, prediction is
unavailable in this setup.

Inputs:

| Argument | Value |
| --- | --- |
| `template_id` | Required. One of the template IDs in the tool schema. Each template fixes the outcome and the horizon |
| `asset_ids` | Optional asset scope. Omit it to score every asset the tool covers. Otherwise pass asset IDs exactly as the user or an earlier result gave them |

There is no horizon, anchor, or query argument. The template sets the horizon,
and the tool sets the anchor.

Result fields:

| Field | Meaning |
| --- | --- |
| `available` | `false` when the prediction service could not be reached; `reason` says why |
| `template_id` | The template that ran |
| `pql` | The exact predictive query that ran |
| `anchor` | The point in time the prediction starts from |
| `horizon` | How far past the anchor the outcome is counted, with its unit |
| `rows` | One row per asset: `asset_id` and `probability` (0 to 1) |
| `model` | The model that scored the query, `kumo-relational` |
| `evidence_id` | The citation ID the application adds to a successful result |

## Procedure

1. Match the question to one template by its outcome, using the template
   descriptions in the tool schema. If no template matches, say which outcomes
   the tool can predict, and do not call it.
2. Set the asset scope. Omit `asset_ids` for "every asset" or a ranking across
   all assets. For named assets, pass their IDs.
3. Call `predict_asset_outcomes` once.
4. Count the call as a successful prediction only when `available` is not
   `false` and `rows` holds a finite numeric `probability` for each asset in
   scope.
5. If `available` is `false`, or the tool is not in your tool list, do not
   retry. Report prediction as unavailable and continue with the other work
   items.
6. Report each probability with the result's `anchor` and `horizon`.

## Pitfalls

- The template fixes the horizon. If the question asks for a different window,
  say the tool predicts only the template's horizon, and state that horizon. Do
  not relabel it.
- Use the horizon's unit exactly as returned. Calendar days are not trading
  sessions.
- Never replace a failed prediction with historical data, and never present a
  historical measure as a prediction.
- Probabilities are model predictions, not certainties.
- Do not write PQL and do not pass it to the tool. Only the curated templates
  run.
- For "observed versus predicted" questions, get the history as a separate work
  item and join the two results on asset ID.

## Example

Question: "Which assets are most likely to have a positive return over the next
5 calendar days?"

```
predict_asset_outcomes(template_id="<the template ID for a positive return, from the tool schema>")
```

Rank the assets by `probability`, state the anchor and horizon, and cite the
result's `evidence_id`.
