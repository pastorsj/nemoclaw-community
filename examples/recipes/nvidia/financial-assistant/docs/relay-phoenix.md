<!--
  SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
  SPDX-License-Identifier: Apache-2.0
-->

# NeMo Relay and Phoenix

Hermes `0.20.0` loads NeMo Relay `0.7.2` in process through Hermes's native
`observability/nemo_relay` plugin. Hermes owns the session, model, and tool
lifecycles that Relay observes. The recipe does not install a custom Relay
extra or plugin and does not run a Relay daemon, shell-forwarding hook, or
finalization helper.

Relay has two local outputs:

- Agent Trajectory Interchange Format (ATIF) records written or refreshed
  under `/sandbox/.hermes-data/atif` after completed Hermes turns.
- OpenInference spans sent by OpenTelemetry Protocol (OTLP) over HTTP to the
  host Phoenix `17.13.0` collector.

There is no S3, MinIO, remote HTTP ATIF, or other object-storage target.

## Data flow

```text
Hermes session, model call, and tool call
                |
                v
native observability/nemo_relay plugin
        |                         |
        | session-end snapshot    | OpenInference spans
        v                         v
  /sandbox/.hermes-data/atif/*.json  host.openshell.internal:6006/v1/traces
                                  |
                                  v
                         Phoenix on 127.0.0.1:6006
```

The Relay configuration is immutable in the sandbox image. Hermes selects it
through `HERMES_NEMO_RELAY_PLUGINS_TOML` and enables only the bundled native
plugin. OpenShell policy allows the collector route from the sandbox. Docker
publishes the Phoenix interface and HTTP collector only on host loopback.

Do not publish port `6006` as a cloud-workspace endpoint or change its host
binding to `0.0.0.0`. This local Phoenix deployment has no recipe-managed user
authentication. If remote access is required, deploy an approved
authentication, authorization, TLS, and tenant boundary instead of weakening
the loopback binding.

## ATIF session boundary

After a completed API or chat turn, Hermes emits session-end and the native
plugin exports or refreshes the session's ATIF trajectory. Hermes finalize and
reset events close the Relay session. You do not need to finalize the session
to download the latest completed-turn snapshot. An abrupt process or sandbox
termination can omit events from the in-flight turn.

Complete at least one turn, then download the sandbox-local records:

```console
$ bash scripts/download-traces.sh
```

The script copies `/sandbox/.hermes-data/atif` to
`.traces/atif-<timestamp>.tar.gz`. An empty archive means no completed Hermes
turn has produced ATIF yet. The source directory is ephemeral and is removed
with the sandbox.

## Phoenix behavior

`scripts/bring-up.sh` starts `arizephoenix/phoenix:17.13.0` through
`observability/phoenix-compose.yml` and builds the sandbox with this collector
endpoint:

```text
http://host.openshell.internal:6006/v1/traces
```

Open Phoenix from the same host at `http://127.0.0.1:6006`. Select the project
named by `PHOENIX_PROJECT_NAME` (`financial-assistant` by default) to inspect
agent, model, and tool spans. Phoenix is an operational debugging view. It is
not proof that an answer is correct, an immutable audit store, or a regulated
recordkeeping system.

Compose binds both the Phoenix interface and OTLP/HTTP receiver to
`127.0.0.1:6006` and persists state in the `phoenix-data` Docker volume. The
recipe does not enable Phoenix authentication because the service is local and
single-operator. Loopback must remain the host access boundary.

ATIF and Phoenix are complementary. ATIF is refreshed at the completed-turn
session-end boundary and closed on finalize or reset. Phoenix receives spans
while work runs. A Phoenix delivery failure does not create a remote ATIF
fallback, and this recipe does not upload either format outside the host.

## Temporary Hermes overlay

The NemoClaw `v0.0.105` Hermes base contains Hermes `0.19.0`. This recipe
replaces that installation with checksum-pinned Hermes commit
`a1bfbccc02d5bfdaef1568facfca2cc1456c59f0` (package `0.20.0`). That upstream
snapshot already defines the compatible `nemo-relay>=0.7.1,<0.8` range and the
dependency security floor. Its lock selects `0.7.1`, so the recipe applies a
narrow lock-only patch for NeMo Relay `0.7.2`.

The overlay changes the version of Hermes already present in the base image;
it does not patch a downloaded NemoClaw source tree. The Relay package remains
a normal Hermes dependency. Remove the lock patch when Hermes locks a reviewed
Relay `0.7.2` or newer in the compatible range, and remove the overlay after a
released NemoClaw base contains the reviewed runtime versions.

## Verify observability

Run the recipe verification after bring-up:

```console
$ bash scripts/verify.sh
```

The observability checks establish that:

- the offline contract retains the native plugin, local-only ATIF, loopback
  Compose binding, and one policy-scoped Phoenix `POST /v1/traces` route;
- the installed packages report Hermes `0.20.0` and NeMo Relay `0.7.2`;
- Hermes enables the native `observability/nemo_relay` plugin;
- Relay accepts the immutable configuration;
- no standalone Relay process is running;
- a real tool-using Hermes API turn creates local ATIF with terminal evidence;
- Phoenix receives one correlated trace whose LLM spans and terminal-tool span
  all have `OK` status.

If verification fails, do not describe the deployment as traced. Inspect the
reported sandbox or Phoenix log, correct the cause, and run the complete check
again.

## Privacy and cleanup

ATIF and Phoenix can contain full prompts, generated text, tool arguments,
public-data results, errors, timestamps, and model metadata. Do not enter API
keys, account data, material nonpublic information, or personal financial
records into the assistant. Loopback limits network reachability but does not
remove sensitive content from traces or protect it from another user who
controls the host.

Download only the records that must be retained. Before destroying the
sandbox, capture them if needed. Then choose the teardown scope:

```console
$ bash scripts/download-traces.sh
$ bash scripts/tear-down.sh                       # keep Phoenix running
$ bash scripts/tear-down.sh --stop-host-services  # stop Phoenix; keep its volume
$ bash scripts/tear-down.sh --purge-host-services # stop Phoenix; delete its volume
```

All teardown modes remove the sandbox but leave the gateway-level inference
route and provider configured, avoiding a dangling provider reference. None
deletes `.traces/`, verification output in `.tmp/`, `.env`, or the built Docker
image. Restore any prior gateway route before deleting the financial provider;
remove other retained artifacts separately when their retention period ends or
the deployment is retired.
