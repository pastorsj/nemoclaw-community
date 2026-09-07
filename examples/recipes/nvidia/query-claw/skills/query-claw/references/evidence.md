# Multi-source evidence

Build a compact ledger before combining views:

| Claim | Kind | Evidence view | Source ID or locator | As of | Limitation |
|---|---|---|---|---|---|
| exact record or total | Observed | records | entity/query ID | source time | truncation |
| document statement | Observed | documents | document + locator | document time | coverage |
| future outcome | Predicted | predictions | target + entity ID | anchor time | horizon |
| arithmetic result | Calculated | none | input claim IDs | calculation time | assumptions |

Join views only on confirmed shared IDs. Report conflicts instead of choosing a
convenient value. An attribution describes model influence, not causation. If
one view fails, return a partial answer only when its reduced scope is explicit.

Include decisive evidence and uncertainty, not raw traces or hidden reasoning.
End with the `Sources used:` line defined by the coordinator skill.
