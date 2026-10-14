# UI

The demo's web app: a Next.js 16 / React 18 UI built on the upstream
[AI-Q UI](https://github.com/NVIDIA-AI-Blueprints/aiq/tree/develop/frontends/ui)
and the NVIDIA KUI component library. [UPSTREAM.md](UPSTREAM.md) records the base
commit and every change from it.

It has three pages:

- `/`: the landing page. In live mode it lists the active data pack's featured
  questions (`GET /v1/pack`); each opens `/research` with the question in the composer.
- `/research`: the chat. Every question is a durable job on the API, followed
  over Server-Sent Events until its answer arrives. Cited evidence opens in the
  execution view.
- `/api/*`: the server routes below.

## How it fits

```
browser ──► ui (this) ──/api/v1/*──► api:8000 ──► Hermes (OpenShell sandbox)
                │
                └──/api/recordings/*──► /packs/$DATA_PACK/recordings (read-only)
```

The browser only talks to this origin. The server routes are:

| Route                        | Purpose                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/health`            | UI liveness, for the container healthcheck. Never calls the API.                                                                                                                                                                                                                                                                                                          |
| `GET, POST /api/v1/<path>`   | Proxy to `$API_URL/v1/<path>`, limited to `pack`, `data_sources/**` (and `POST data_sources/{id}/query`), `POST jobs/async/submit`, `GET jobs/async/job/{id}/**`, `POST jobs/async/job/{id}/cancel` and `…/benchmark`, and `POST speech/transcriptions` (a WAV of at most 3 MiB). Anything else, including `/internal/**`, is a 404. SSE streams pass through unbuffered. |
| `GET /api/recordings/<path>` | Files of the active pack's replay bundle (`.json`, `.jsonl` only).                                                                                                                                                                                                                                                                                                        |

## Modes

`UI_MODE=live` (default) submits questions to the API. `UI_MODE=replay` shows
only the recorded sessions of the data pack, under "Recorded" as the original
demo UI did: the composer and the data source selection are read only, the data
sources come from the bundle's `pack.json`, and the `/api/v1` proxy answers 404
without calling the API.

## The execution view

The execution view (graph, capability explorers, timeline, replay) lives in
`src/features/execution` and plugs in through one typed interface,
`ExecutionFeature` in `src/shared/context/ExecutionFeatureContext.tsx`:

| Slot                | Used for                                                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `onJobEvent(event)` | Every SSE record of a live job, in order (the event names in `JOB_STREAM_EVENTS`, `adapters/api/deep-research-client.ts`) |
| `Workspace`         | The view opened by "View Execution" or a cited evidence source                                                            |
| `ActivityPanel`     | The "Agent Activity" side panel's tabs: Thinking, Timeline and Benchmark                                                  |
| `recordings`        | `list()` and `load(id)` of recorded sessions in replay mode                                                               |

`src/app/providers.tsx` wires the implementation. Until it exists, the UI runs
with `noExecutionFeature` and simply hides those parts.

The execution graph is a fixed topology drawn with plain HTML and SVG, with its
own pan and zoom. The explorers and the data viewer are drawn the same way, with
no graph library.

## Configuration

Runtime environment, read per request (see [.env.example](.env.example)):

| Variable                                           | Default            | Meaning                                                                                           |
| -------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------- |
| `UI_MODE`                                          | `live`             | `live` or `replay`                                                                                |
| `API_URL`                                          | `http://api:8000`  | Demo API, server-side only                                                                        |
| `PACKS_DIR`                                        | `/packs`           | Directory of data packs                                                                           |
| `DATA_PACK`                                        | `synthetic-market` | Active pack; recordings come from `$PACKS_DIR/$DATA_PACK/recordings`                              |
| `PHOENIX_URL`                                      | unset              | Browser-reachable Phoenix UI; unset hides the Phoenix link                                        |
| `SPEECH_INPUT_ENABLED`, `SPEECH_INPUT_MAX_SECONDS` | `false`, `60`      | The composer's microphone (live mode only; the API transcribes), and the longest recording        |
| `PORT`, `HOSTNAME`                                 | `3000`, `0.0.0.0`  | Listen address of the container's server (`npm start` and `npm run dev` listen on 127.0.0.1 only) |

Icons load from NVIDIA's brand-asset CDN, so the browser needs internet access.

## Run

```bash
npm ci
cp .env.example .env.local    # then adjust
npm run dev                   # http://127.0.0.1:3000
```

Production build, as in the container:

```bash
npm run build                 # .next/standalone, with static assets copied in
npm start                     # http://127.0.0.1:3000 (set PORT to change the port)
docker build -t market-demo/ui:local .
```

In the full stack, `compose.yaml` runs this image as the `ui` service.

## Check

```bash
npm run lint
npm run type-check
```

This copy of the UI omits its test suites.
