---
name: searching-documents
description: Finds cited passages in the selected document sources
license: Apache-2.0
compatibility: Requires the retrieval MCP server (retrieve_evidence tool)
metadata:
  author: NVIDIA
  version: "1.2"
  hermes:
    tags:
      - retrieval
      - documents
      - citations
      - nemotron
    related_skills:
      - analyzing-market-data
      - querying-auto-ontology
---

# Searching documents

`retrieve_evidence` searches every document source selected for this turn. It
ranks passages with NVIDIA Nemotron retrieval models and returns up to 8
passages with their titles, citations, and dates.

## When to Use

- The selected sources include the `unstructured_retrieval` capability, and
- the answer depends on what a document says: a filing, disclosure, regulation,
  policy, or quotation.

Numbers calculated from data belong to `analyzing-market-data` or
`querying-auto-ontology`.

## Tool

`retrieve_evidence` (Hermes tool ID `mcp__retrieval__retrieve_evidence`)

| Argument | Value |
| --- | --- |
| `query` | A focused description of the passage you need |
| `top_k` | Leave unset: it returns 8 passages, the most one call returns |

The application limits the search to the selected sources. Pass only `query`.

## Procedure

1. Write one focused query per distinct topic, in the words the passage itself
   would use. Name the concept you need, not the answer you expect. To find one
   company's documents, put its name in the query.
2. When you search filings, leave form names (8-K, 6-K) and words such as
   "SEC filing", "current report" or "material event" out of the query. Every
   filing's cover page repeats them, so a query with them returns cover pages
   instead of what the filings report. Each passage's metadata gives its form
   and filing date: check the form and period there. Item numbers and titles,
   such as "Item 1.05 material cybersecurity incidents", are content: keep
   them. A search of a rule may name the form the rule governs.
3. For a kind of event, such as disruptions, incidents or restructurings,
   write the query as the sentence a filing would use to report one, such as
   "a fire damaged the plant" or "the company will close a facility and cut
   jobs". A list of keywords, or the category's name alone, matches the risk
   lists of forward-looking statements, which say what could happen, not what
   did.
4. Make one call even when several document sources are selected. Their
   passages are ranked together.
5. Keep only passages that directly support a claim, and note each passage's
   title, citation, and date.
6. If nothing relevant comes back, rephrase once with different key terms. Then
   say the passages found do not cover the question. A call returns only its
   best passages, so never claim that a source contains no such document.
7. That makes at most two searches per topic. A question about a rule and the
   filings that apply it has two topics: search the rule, then the filings.
   When the passages cover only part of a rule, cite what they show and name
   what is missing rather than searching again.

## Pitfalls

- Keep what a document's author claims separate from what a rule or policy
  requires.
- Quote exact wording only when the wording matters. Otherwise paraphrase and
  cite.
- Never answer a document question from general knowledge or web search.
- Link a document to a stock in the market data only through the passage's
  `ticker` metadata. A matching company name alone does not prove it is the
  same entity.

## Example

Question: "What must a company disclose after a material cybersecurity
incident, and how quickly?"

```
retrieve_evidence(query="disclosure requirements and deadline after a material cybersecurity incident")
```

Question: "Which second-quarter current reports describe a product recall?"

```
retrieve_evidence(query="the company recalled products after a defect was found")
```

The query is one sentence that reports the event, and it names no form. A list
such as "product recall 8-K defect safety notice" returns cover pages and risk
lists instead: the form name matches every cover page, and the keywords match
the risks that forward-looking statements list. The passages' metadata gives
each filing's form and date.

Answer from the returned passages and cite the result's `evidence_id` after each
claim as `[evidence:<evidence_id>]`. One token covers every passage of the
result: do not add a passage's rank or title inside the brackets.
