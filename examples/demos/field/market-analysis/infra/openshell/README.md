# OpenShell

The Hermes agent runs inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
0.1.2 sandbox. This directory holds the pieces that run it with official
OpenShell artifacts only: the gateway image, the supervisor and sandbox runtime
images, and the CLI release tarball.

```text
api ──▶ hermes-gateway (openshell forward service) ──gRPC/mTLS──▶ openshell gateway :18080
                                                                    │ Docker driver
                                                                    ▼
                             supervisor container (host network) ◀─▶ sandbox (network=none)
                                        │                            Hermes on 127.0.0.1:8642
                                        ▼
         host.openshell.internal = 127.0.0.1 ─▶ Switchyard :4000, API :8000, MCP :8120/:3010/:3003, Phoenix :6006
```

- The sandbox has no network interface. Every connection Hermes opens is
  checked against the sandbox policy (`agent/sandbox-policy.yaml`) plus the
  attached provider profiles, then made by the host-networked supervisor.
- Because `grpc_endpoint` is the IP literal `127.0.0.1`, the supervisor maps
  `host.openshell.internal` to host loopback. Services the sandbox calls must be
  published on `127.0.0.1:<port>`.
- The sandbox has no inbound ports either, so the API reaches Hermes through
  `openshell forward service`, run by the `hermes-gateway` container.

## Files

| File | Purpose |
|---|---|
| `versions.env` | The OpenShell pin: version, three image digests, two CLI checksums. |
| `gateway.toml` | Gateway config (schema v2): bind `0.0.0.0:18080` in its container, health on `:18081`, `grpc_endpoint` `https://127.0.0.1:18080`, runtime images by digest, sandbox namespace `market-demo`, bind mounts off. |
| `cli.Dockerfile` | CLI image from the sha256-checked release tarball (amd64, arm64). Runs one-off commands and the forwarder. |
| `providers/switchyard.yaml` | Credential-free inference provider: `POST /v1/chat/completions` and `GET /v1/models` on `host.openshell.internal:4000`. |
| `providers/receipts.yaml` | Binds `HERMES_RECEIPT_API_KEY` to the API's three `/internal/hermes` routes on `:8000` (execution-scope, tool-receipts, llm-calls). Hermes sees only a placeholder; the supervisor substitutes the real key on those routes. |

## Compose contract

Every resource belongs to the Compose project `market-demo`. `docker compose
--env-file infra/openshell/versions.env` makes the pins available for
interpolation (for example `image: ${OPENSHELL_GATEWAY_IMAGE}`).

| Service | Image | Settings |
|---|---|---|
| `openshell-certs` (one-shot) | gateway | `user: "0"`, `network_mode: none`, `command: [generate-certs, --output-dir, /var/lib/openshell/tls]`, `XDG_CONFIG_HOME=/client`, volumes `openshell-state:/var/lib/openshell`, `openshell-client:/client`. Idempotent. |
| `openshell-preflight` (one-shot) | gateway | `user: "0"`, `network_mode: none`, `command: [config, preflight, --path, /etc/openshell/gateway.toml]`, `gateway.toml` mounted read-only. |
| `openshell` | gateway | `user: "0"` (Docker socket), `command: [--config, /etc/openshell/gateway.toml]` (overrides the image's `--bind-address 0.0.0.0 --port 8080`), ports `127.0.0.1:18080:18080` and `127.0.0.1:18081:18081`, env `OPENSHELL_DB_URL=sqlite:/var/lib/openshell/gateway.db?mode=rwc`, `OPENSHELL_LOCAL_TLS_DIR=/var/lib/openshell/tls`, `OPENSHELL_TELEMETRY_ENABLED=false`, `XDG_STATE_HOME=/var/lib/openshell`, volumes `/var/run/docker.sock`, `openshell-state`, `gateway.toml:ro`. Depends on both one-shots. |
| `openshell-cli` (tools profile) | `cli.Dockerfile` | `user: "0"`, env `OPENSHELL_GATEWAY=openshell`, `OPENSHELL_GATEWAY_ENDPOINT=https://openshell:18080`, `XDG_CONFIG_HOME=/client`, volumes `openshell-client:/client` and `./infra/openshell/providers:/providers:ro`, on the gateway's network. `generate-certs` puts the CLI's mTLS bundle in that volume, and `openshell` is a default SAN of the gateway certificate. |
| `hermes-gateway` | `cli.Dockerfile` | As `openshell-cli`, plus the default network, `restart: unless-stopped` and `command: [forward, service, hermes, --target-port, "8642", --local, "0.0.0.0:8642"]`. Start it only after the sandbox is Ready: it exits when the sandbox is not, and the restart policy brings it back once the sandbox is Ready again, for example after a gateway restart. Health: `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/capabilities` returns 200 or 401. |
| `agent` (build profile) | `agent/Dockerfile` | `image: market-demo/hermes-sandbox:local`, build context `./agent`, `additional_contexts: {contracts: ./contracts}`, build arg `AGENT_FEATURES`. `demo.sh` derives it from `COMPOSE_PROFILES`: keep `retrieval`, `analytics` and `ontology`, map `analytics-gpu` to `analytics`, and ignore every other profile; `kumo` is added when `KUMO_RELATIONAL_URL` and `KUMO_API_KEY` are both set. For example, `core,retrieval,analytics-gpu` with both set becomes `retrieval,analytics,kumo`. |

The gateway health endpoint is `http://127.0.0.1:18081/readyz`.

## Bring-up

`demo.sh` runs all of this. By hand, from the repository root, Compose needs the
OpenShell pins as well as `.env` (a bare `docker compose` fails on the empty
image names). These helpers work in bash and zsh:

```bash
dc() { docker compose --env-file infra/openshell/versions.env --env-file .env "$@"; }
cli() { dc run --rm -T openshell-cli "$@"; }
```

This is the sequence proven on Docker 28.4 (Ubuntu 24.04 arm64, kernel 6.8):

```bash
set -a; . infra/openshell/versions.env; set +a          # the image pins, for the two pulls
docker pull "$OPENSHELL_SUPERVISOR_IMAGE"; docker pull "$OPENSHELL_SANDBOX_IMAGE"   # else the gateway pulls at start
dc up -d openshell                                      # certs and preflight run first
curl -fsS http://127.0.0.1:18081/readyz

cli profile lint -f /providers/switchyard.yaml
cli profile import -f /providers/switchyard.yaml        # create-only; see "Updating profiles"
cli profile import -f /providers/receipts.yaml
cli provider create --name switchyard --type switchyard
dc run --rm -T -e HERMES_RECEIPT_API_KEY openshell-cli \
  provider create --name receipts --type receipts --credential HERMES_RECEIPT_API_KEY

# Start Switchyard, the MCP servers, Phoenix and the API first: Hermes parks an
# MCP server it cannot reach at startup.
dc run --rm -T -e HERMES_API_SERVER_KEY --entrypoint sh openshell-cli -c \
  'exec openshell sandbox create --name hermes --from market-demo/hermes-sandbox:local \
     --provider switchyard --provider receipts --no-auto-providers \
     --label demo.fingerprint=<hash> --env "API_SERVER_KEY=$HERMES_API_SERVER_KEY" \
     --no-credential-warnings --detach --no-tty -- /opt/hermes/.venv/bin/hermes gateway run'
cli sandbox get hermes -o json                          # poll until .phase == "Ready" (about 5 s)
dc up -d hermes-gateway
```

- `--credential KEY` and `sh -c` with `-e KEY` keep secret values off the host
  command line.
- `.env` holds the Runs API key as `HERMES_API_SERVER_KEY`; Hermes reads it as
  `API_SERVER_KEY`. It must be at least 16 characters: with a shorter or empty
  key Hermes' API server does not start, and the `hermes-gateway` health check
  never passes. It cannot be a provider placeholder, because Hermes checks it
  on inbound requests and placeholders resolve only on outbound ones.
- `sandbox create --detach` returns once the container is created. Poll
  `sandbox get` for `Ready`, then allow Hermes about 10 s to bind `:8642`.
- A gateway restart restarts the sandbox's containers, and with them Hermes.
- `sandbox exec` processes do not inherit the image's `ENV`; the sandbox's main
  process does.

## Teardown

```bash
# first: step 2 of "Leaked volumes" below
cli sandbox delete hermes       # before stopping the gateway; gone in about 5 s
# then: step 3 of "Leaked volumes"
dc down -v                      # never --remove-orphans on a shared project
```

### Leaked volumes

`sandbox delete` removes both containers and the driver's volumes, but
OpenShell 0.1.2 removes containers without their anonymous volumes. The Hermes
image declares `VOLUME /opt/data`, so every sandbox leaves two unlabelled
volumes: the workload's, and one from the identity-resolver container that the
driver creates from the image while provisioning. Nothing links either volume
to the sandbox once its container is gone, so record their names while that is
still possible, and remove only those. Never scan all dangling volumes: the
Docker host is shared, and most of them belong to other projects.

```bash
dangling() { docker volume ls -q -f dangling=true | sort; }
state=.demo/openshell-volumes   # any file in the lifecycle state

# 1. Around sandbox create: once the sandbox is Ready, the identity volume is the new dangling one.
dangling > "$state.before"
#    ... sandbox create, then poll until Ready ...
comm -13 "$state.before" <(dangling) >> "$state"

# 2. Just before sandbox delete: the workload's volume, from its labelled container.
workload=$(docker ps -aq -f label=openshell.ai/sandbox-namespace=market-demo \
  -f label=openshell.ai/sandbox-name=hermes -f label=openshell.ai/isolation-role=sandbox)
[ -z "$workload" ] || docker inspect \
  -f '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Name}}{{end}}{{end}}' "$workload" >> "$state"

# 3. Once sandbox delete has removed the containers (no container labelled
#    openshell.ai/sandbox-namespace=market-demo is left): remove only the recorded
#    volumes, and only those that hold this profile.
while read -r v; do
  docker volume inspect "$v" >/dev/null 2>&1 || continue   # docker run -v would create it
  docker run --rm --network none -v "$v:/v:ro" --entrypoint grep market-demo/hermes-sandbox:local \
    -qsx 'name: market-analysis-agent' /v/distribution.yaml && docker volume rm "$v"
done < "$state"
rm -f "$state" "$state.before"
```

Step 1 may also catch a volume another project left dangling at the same
moment; the `distribution.yaml` check in step 3 skips it.

### Idle upstream connections

The 0.1.2 proxy keeps a sandbox connection's upstream connection open after the
server closes it for being idle, and the next request on that connection fails
with no response. Clients that retry recover. NeMo Relay does not, so it dropped
the trace batch after every long model call, including the job's turn span. Phoenix
therefore runs with a 120 s keep-alive (`infra/phoenix/serve.py`), longer than
Relay keeps an idle connection.

## Updating profiles

`profile import` is create-only, `profile delete` fails while any provider uses
the profile, and `profile update <ID> --file <FILE>` requires the current
`resource_version`. Prepend it to the file from this directory:

```bash
dc run --rm -T --entrypoint sh openshell-cli -c \
  'v=$(openshell profile export receipts -o json | jq -r .resource_version)
   { echo "resource_version: $v"; cat /providers/receipts.yaml; } > /tmp/profile.yaml
   openshell profile update receipts --file /tmp/profile.yaml'
```

`provider update <NAME> --credential KEY` rotates a credential in place.

## Host requirements

Docker Engine 28 or later, and a Linux kernel with Landlock ABI 3 or later
(6.2+). The sandbox probes seccomp user notification and Landlock itself and
fails closed. On Docker Desktop, enable host networking and turn off Enhanced
Container Isolation. amd64 and arm64 both work.

## Bumping OpenShell

1. Take the new digests from anonymous GHCR manifest requests and the CLI
   checksums from the release's `openshell-checksums-sha256.txt`; update
   `versions.env`.
2. Update the two runtime images in `gateway.toml`.
3. Check that the image references in both files agree, rebuild the CLI image,
   and recreate every sandbox: an old supervisor must not outlive a gateway
   upgrade.
4. If the proxy now closes a sandbox connection when its upstream goes idle,
   drop `infra/phoenix/serve.py` (see "Idle upstream connections").

## Troubleshooting

| Symptom | Look at |
|---|---|
| Gateway not ready | `./scripts/demo.sh logs openshell-preflight openshell` |
| Sandbox stuck in `Provisioning` | `cli sandbox get hermes -o json` (`conditions`), `cli logs hermes` |
| A tool call fails while the sandbox is Ready | `endpoint_statuses` in `sandbox get`; `cli logs hermes --since 5m` shows `DENIED` lines with the binary, host and reason |
| Receipts return 403 `credential_endpoint_mismatch` | The request left the routes bound in `providers/receipts.yaml` |
| Effective policy | `cli policy get hermes --full` (includes the `_provider_*` rules) |
