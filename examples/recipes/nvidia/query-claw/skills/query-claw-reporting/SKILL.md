---
name: query-claw-reporting
description: Present already-retrieved Query Claw evidence as a concise answer, Markdown table or report, or small text chart. Use when the operator requests an output format; do not retrieve missing evidence or create HTML, SVG, PowerPoint, or other files.
---

# Query Claw reporting

Format only evidence already returned by the selected Query Claw views. Do not
query another source merely to fill a layout, and do not blur observed,
predicted, or calculated values.

Choose the requested supported form:

- **Concise answer:** one to three direct sentences, followed by material
  uncertainty.
- **Markdown table:** one row per comparable entity; include units, evidence
  kind, source ID or locator, and as-of time when available.
- **Markdown report:** use `Summary`, `Findings`, `Evidence`, `Uncertainty`, and
  `Next step` headings. Omit an empty section except `Uncertainty`.
- **Text chart:** show at most 10 sorted values with labels, exact values and
  units, and fixed-width bars scaled to the largest non-negative value. State
  the scale; use a table instead when values are negative or not comparable.

Do not generate HTML, SVG, PowerPoint, or other files in this pass. Offer a
Markdown report or text chart instead. Preserve evidence citations and end
with the coordinator's `Sources used:` line, naming only the views actually
queried. Use `Sources used: none` if no evidence source was queried.
