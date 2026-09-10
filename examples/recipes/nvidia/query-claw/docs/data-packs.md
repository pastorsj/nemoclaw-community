<!--
  SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
  SPDX-License-Identifier: Apache-2.0
-->

# Query Claw Data Packs

A data pack describes one dataset and the evidence routes Query Claw may use
for it. Packs make dataset selection explicit; they do not replace loading a
structured database into NVIDIA Ontology or configuring its prediction path.
The recipe generates and installs only the built-in `supply-chain` pack.

## Pack Layout

Place each pack at `<packs-root>/<id>/pack.json`. The ID must be safe
kebab-case and match the directory name. Paths in `views` must be existing,
contained, relative POSIX paths. Packs and their contents must not use
symlinks.

```text
/srv/query-claw/packs/cloud-operations/
├── pack.json
├── structured/
└── documents/
```

```json
{
  "schema_version": 1,
  "id": "cloud-operations",
  "title": "Cloud Operations",
  "description": "Service telemetry for operational investigation and forecasting.",
  "industry": "Cloud Services",
  "views": {
    "structured": "structured",
    "documents": "documents",
    "predictions": "structured"
  },
  "bindings": {
    "ontology": {
      "database": "cloud_operations",
      "prediction_database": "cloud_operations",
      "prediction_probe": "Which active services are most likely to have an incident in the next 7 days?"
    },
    "retriever": {
      "collection": "query-claw-cloud-operations"
    }
  }
}
```

The configured views determine the required bindings:

| Pack view | Agent-facing view | Binding | Meaning |
| --- | --- | --- | --- |
| `structured` | `records` | `ontology.database` | Governed structured questions through NVIDIA Ontology. |
| `documents` | `documents` | `retriever.collection` | Cited unstructured retrieval from one NeMo Retriever collection. |
| `predictions` | `predictions` | `ontology.prediction_database` | Predictions through NVIDIA Ontology and its configured Kumo integration. Requires one real, non-destructive `prediction_probe` used only during setup qualification. |

At least one view is required. Include exactly the bindings needed by the
declared views. `prediction_database` is optional and defaults to `database`.
The `industry` value describes the pack for operators; it does not select or
authorize a dataset.

## Add An Industry Dataset

The same contract works for education or research, automotive, cloud,
manufacturing, semiconductor, and other industry data:

1. Confirm that the source data can be used and redistributed for the intended
   deployment.
2. Create a direct `<id>` child under your pack registry and add only the files
   that deployment needs.
3. Add `pack.json` with the available views and their service bindings.
4. For `structured` or `predictions`, separately load the named database into
   NVIDIA Ontology, complete semantic compilation, and configure its Kumo
   prediction integration when predictions are declared. Give each predictive
   pack one stable question that its graph supports in `prediction_probe`;
   setup must receive nonempty Kumo rows and a graph receipt naming that exact
   database. Selecting a pack does not perform those provisioning steps.
5. For `documents`, place the source files under the declared path. Retriever
   setup creates the named collection, ingests those files, and verifies that
   the collection returns evidence.
6. Materialize the selection, then confirm that `check_readiness` reports the
   expected dataset, views, and Ontology readiness before asking questions.

For example, validate and materialize an operator-managed registry without
installing the built-in pack:

```bash
python3 scripts/prepare_data_packs.py \
  --no-install-built-in \
  --packs-root /srv/query-claw/packs \
  --datasets cloud-operations \
  --active-dir .runtime/active-data
```

This command packages an allowlist; it does not provision an Ontology
database. The current one-command `deploy/setup.sh` bootstraps the built-in
supply-chain database, so selections used with that path must still include
`supply-chain`. Provision additional Ontology databases through the service's
administrative workflow before expecting their record or prediction views to
be ready.

## Select Data At Startup

Set the registry and comma-separated allowlist in `.runtime/deploy.env`:

```dotenv
QUERY_CLAW_PACKS_ROOT=/srv/query-claw/packs
QUERY_CLAW_DATASETS=supply-chain,cloud-operations
```

The one-command setup always refreshes its built-in `supply-chain` pack.
Invalid, missing, empty, or duplicate IDs fail setup.

Activation fingerprints `pack.json` plus each declared view, copies only those
files to `.runtime/active-data/packs/<id>/`, and writes
`active-data-packs.json` with rewritten relative view paths. Deployment mounts
that active tree read-only; it does not mount the source registry. Unselected
packs and undeclared files therefore cannot be reached through Query Claw. Do
not put secrets in a pack or a declared view.

Changing `QUERY_CLAW_DATASETS` and rerunning setup replaces the active tree.
Setup stops the MCP facade and its ingress before that replacement; if any
later setup step fails, both remain down rather than serving the prior
allowlist. Use separate deployments when datasets require a hard isolation
boundary.

## Scope A Turn

The facade can issue a short-lived source scope for trusted callers. A scope
names allowed dataset IDs and any subset of `records`, `documents`, and
`predictions`. Pass its opaque `scope_token` to readiness and every data tool.
It can only narrow the deployment allowlist; it cannot activate an unselected
dataset or unavailable view. Every authorized attempt is recorded by tool and
dataset before its upstream call begins, including failed calls, and can be
read before the token is revoked.

Scopes are process-local, expire after at most 3,600 seconds, allow at most
1,000 recorded calls, and disappear on restart. They are not a durable user
identity, persistent authorization policy, or substitute for deployment-level
isolation. Treat each token as a credential, expose it only through the trusted
invocation path that passes it to tools, and never repeat it in answers or
reports.

When one dataset is active, a data tool may infer its ID without a scope. While
any turn scope is active, unscoped calls are blocked so the one-dataset fallback
cannot bypass that scope; revocation or expiry restores dashboard access. A
multi-dataset deployment always rejects unscoped Query Claw calls: a trusted
caller must create a scope for the turn, including an empty deny-all scope when
no source is selected. With multiple scoped datasets, each data call must name
`dataset_id`. If a question could apply to more than one visible dataset,
Hermes asks the operator to choose instead of searching them all. Cross-dataset
analysis should use separate, dataset-bound calls and combine only the returned
evidence.

The stock Hermes dashboard does not create source scopes. For interactive
dashboard use, select one dataset at startup with `QUERY_CLAW_DATASETS`. Use a
trusted API controller—such as the included evaluator—when one deployment needs
per-turn switching across multiple active datasets. The controller, not the
model or browser, owns `/scopes/create`, injects the token into turn
instructions, inspects the call audit, and revokes the token.

## What Setup Loads

Query Claw automatically generates the synthetic supply-chain files, loads
that database into the bundled NVIDIA Ontology service, and ingests active
document views into their declared NeMo Retriever collections. It does not
discover arbitrary databases, infer bindings from file contents, load another
structured database, or create an industry-specific agent profile. A pack is a
declaration and deployment boundary, not a general ETL format.

Keep private or restricted packs outside the repository and point
`QUERY_CLAW_PACKS_ROOT` at them; `.runtime/` is ignored. Before publishing a
pack, verify redistribution rights, retain required attribution and license
files, and remove secrets, customer data, personal data, private endpoints,
and internal identifiers. Public availability or API access alone does not
grant redistribution rights. Prefer appropriately licensed public data or
clearly documented synthetic data.
