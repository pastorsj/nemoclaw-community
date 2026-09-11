# Query Claw skills

Hermes always receives the coordinator and receives a specialist skill only
when the active dataset supports that data path:

| Skill | Path | Official tool |
| --- | --- | --- |
| `query-claw` | Select the minimum evidence paths and combine their results. | None; coordination only. |
| `query-claw-structured` | Retrieve governed records and aggregates. | NVIDIA Ontology/GSF `ask_question`. |
| `query-claw-predictive` | Ask GSF to run a Kumo-backed prediction. | NVIDIA Ontology/GSF `ask_question`. |
| `retriever-mcp` | Retrieve and rerank source-bearing document chunks. | Native NeMo Retriever `query`. |

The `retriever-mcp` directory is copied unchanged from the official NeMo
Retriever repository. Only its query workflow applies here: deployment owns
parsing, chunking, embedding, storage, and collection creation, while native
write tools are disabled. The other three skills are Query Claw routing
guidance. GSF has no upstream agent skill in the pinned source, so the two thin
GSF skills describe its official `ask_question` contract without adding an
adapter. There is no web search, general code execution, direct Kumo, or
alternate data tool in this recipe.
