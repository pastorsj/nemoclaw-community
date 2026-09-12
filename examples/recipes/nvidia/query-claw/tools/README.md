# Query Claw tools

Hermes receives up to two official MCP registrations and authorizes only the
answer paths supported by the active dataset:

| Answer path | Authorized answer tool | What it does |
| --- | --- | --- |
| Unstructured retrieval | `mcp__retriever__query` | Retrieves and reranks source-bearing document hits from the active collection. |
| Structured retrieval | `mcp__gsf__ask_question` | Returns SQL and rows from the active NVIDIA Ontology/GSF semantic layer. |
| Structured prediction | `mcp__gsf__ask_question` with `prediction: true` | Returns `sql` beginning with `PREDICT` for a Kumo attempt and one or more rows that each contain a finite numeric score for success. |

[`tool-contracts.json`](tool-contracts.json) is the machine-checked answer-route
contract. Query Claw authorizes only GSF `ask_question` and Retriever `query`
for answers; GSF owns both structured retrieval and structured prediction
behind its one selected tool. NemoClaw's Retriever registration still reports
the server's other six tools during discovery, but OpenShell denies every call
to them.

Query Claw has no web-search, general code-execution, direct Kumo, or alternate
data tool. Deployment exposes one active industry dataset at a time. Retriever
ingestion is deployment-owned and its native write tools are disabled.
