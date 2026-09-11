# Multi-source evidence

Build a compact ledger before combining views:

| Claim | Kind | Evidence view | Source ID or locator | As of | Limitation |
|---|---|---|---|---|---|
| exact record or total | Observed | records | entity/query ID | source time | truncation |
| document statement | Observed | documents | document + locator | document time | coverage |
| future outcome | Predicted | predictions | returned GSF query + entity ID + finite numeric score | anchor time | horizon and route proof |

Join views only on confirmed shared IDs. Report conflicts instead of choosing a
convenient value. An attribution describes model influence, not causation. Call
a result Kumo-backed only when GSF's returned `sql` begins with `PREDICT` and
every prediction row contains a finite numeric score. A `PREDICT` response
without usable scores is an attempted, unavailable prediction; otherwise mark
the predictive route unconfirmed. If one view fails, return a partial answer
only when its reduced scope is explicit.

Before synthesis, retain at least one decisive fact and returned source ID or
locator from every view used. Include decisive evidence and uncertainty, not
raw traces or hidden reasoning. End with the `Sources used:` line defined by
the coordinator skill.
