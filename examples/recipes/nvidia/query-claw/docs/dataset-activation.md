<!--
  SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
  SPDX-License-Identifier: Apache-2.0
-->

# Query Claw Dataset Activation And Isolation

Query Claw activates exactly one dataset for each deployment. The active
dataset determines which of the recipe's three answer capabilities exist:

| Dataset artifacts | Enabled capability |
| --- | --- |
| Document corpus and Retriever collection | Unstructured retrieval through native NeMo Retriever `query`. |
| Structured source | Structured retrieval through official GSF `ask_question`. |
| Structured source with prediction enabled | GSF-mediated prediction through the same `ask_question`; returned `sql` starting with `PREDICT` proves a Kumo attempt, and one or more rows that each contain a finite numeric prediction score prove success. |

A dataset may be structured-only, document-only, or contain both. Deployment
does not fabricate a missing service or give Hermes a tool for an unavailable
capability.

## External Dataset Contract

Set both of these values in `.runtime/deploy.env`:

```dotenv
QUERY_CLAW_DATASET_REPOSITORY=/private/path/to/aiq-3-booth-demo
QUERY_CLAW_DATASET_MANIFEST=/private/path/to/aiq-3-booth-demo/industries/<industry>/datasets/<dataset>/dataset.json
```

The manifest must be inside the selected repository. It identifies its
industry and dataset, declares its build outputs, and maps those outputs into
optional `structured`, `documents`, and prediction contracts. Query Claw
accepts these built artifacts:

- one DuckDB database paired with one reviewed GSF ontology YAML file;
- one document directory paired with one unique Retriever collection name;
- and, for prediction, one graph JSON file paired with reviewed PQL examples.

Every referenced output must be a contained relative path and must already
exist. Activation rejects missing files, incompatible output kinds, duplicate
IDs, symlinks, an empty document corpus, an unpaired database or ontology, and
a prediction contract without structured data.

## Private Normalized Activation

`deploy/setup.sh` runs activation automatically. To inspect the operation
without starting services, run:

```bash
repository=/private/path/to/aiq-3-booth-demo
manifest="$repository/industries/<industry>/datasets/<dataset>/dataset.json"
python3 scripts/activate_dataset.py \
  --dataset "$manifest" \
  --repository-root "$repository" \
  --output .runtime/active-data
```

The activator copies only declared artifacts into a new staging directory that
the owner and service group can read. It fingerprints their content and
normalized metadata, writes `active-dataset.json`, and atomically replaces the
prior activation. Depending on the selected capabilities, the result has this
shape:

```text
.runtime/active-data/
├── active-dataset.json
├── database/<database-name>.duckdb  # external, or structured/ for bundled CSV
├── ontology/model.gsf.yaml
├── documents/
└── prediction/
    ├── graph.json
    └── pql-examples.json
```

These files stay ignored by Git and are mounted read-only into the services;
Compose adds the invoking user's primary group to only the containers that
consume them.
The same normalized manifest represents both source paths. Its database
`engine` is either `duckdb` or `postgres-csv`; prediction `mode` is either
`reviewed` or `native`. Consumers reject other values. The manifest records
only the active dataset, enabled artifacts, collection/database names, source
evaluation paths, and a content fingerprint. It contains no credentials.

## What Deployment Does

For an external DuckDB activation, setup resets the entire GSF catalog owned
by this Query Claw Compose project, imports the reviewed ontology, ingests only
that database's metadata, and seeds reviewed PQL examples. The prediction graph
is retained as activation provenance; the pinned GSF revision derives the
operational graph from its catalog. For the bundled Postgres CSV source, setup loads the staged
CSVs and uses GSF-native semantic compilation and GSF-mediated prediction.

When prediction mode is `reviewed`, deployment derives a bounded scope summary
for Hermes from the reviewed graph and PQL-example metadata. It contains only
optional anchor, entity, eligible-population name and count, and at most eight
reviewed prediction-target names, with a 4 KiB limit. It contains no PQL, graph,
row data, or prediction result; Hermes treats it as data, and may use it only to
explain available prediction scope. GSF still owns query generation and
execution.

For a document activation, setup creates the declared collection and submits
the corpus through NeMo Retriever's native service. Retriever owns parsing,
chunking, embedding, vector storage, and ingestion status. Query Claw verifies
the collection before starting Hermes. At query time only
`mcp__retriever__query` is authorized for answers; the other six tools remain
discoverable through the NemoClaw-owned native registration, but
OpenShell blocks calls to them. Queries use the active collection,
`format="hits"`, and the configured reranker.

GSF and Retriever are independent. A structured-only dataset starts GSF but
does not advertise prediction or require Kumo. A document-only dataset starts
Retriever but not GSF or Kumo. A dataset with structured, prediction, and
document artifacts exposes the two official MCP registrations and enables all
three answer paths. Prediction is successful only when GSF returns `PREDICT`
plus finite numeric scores.

## Isolation Boundary

One deployment has one active dataset. Its selected structured source,
ontology or native semantic mode, prediction support, and document corpus are
the only data artifacts mounted for that activation. The reversible Hermes
profile includes only the servers that activation enables and supplies the
exact active dataset-to-collection mapping.

Official GSF tools do not accept a `dataset_id`, `scope_token`, or `target_db`.
NeMo Retriever's query receives only the active collection. There is no in-chat
database switch and no prompt-only mechanism for hiding another mounted data
source. Use separate deployments for audiences that require distinct data
visibility. If setup fails after a switch starts, the MCP query surface remains
down rather than serving the prior activation.

Treat every setup or dataset change as a maintenance window. Do not run chat
requests concurrently, and start a new chat after setup succeeds so Hermes
loads the restored services, active dataset, and matching skills together.

## Bundled Sample

Leaving both external dataset variables blank activates the repository's
synthetic supply-chain sample. It creates structured records, held-out
prediction labels, and one document collection, then materializes the same
`active-dataset.json` contract as an external source. It is not a template for
mounting several datasets into one agent.

## Data Handling

Keep private or restricted repositories and generated outputs outside the
public checkout. `.runtime/` is ignored, but operators remain responsible for
host access and backups. Before publishing any corpus or manifest, verify
redistribution rights, preserve required attribution and license files, and
remove secrets, customer data, personal data, private endpoints, and internal
identifiers. Public availability or API access alone does not grant
redistribution rights.
