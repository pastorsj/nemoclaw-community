# Upstream

This UI is derived from the AI-Q blueprint UI (Apache-2.0, see [LICENSE](LICENSE)):

- Repository: https://github.com/NVIDIA-AI-Blueprints/aiq, path `frontends/ui`
- Base: `develop` at `bf4e67d1564ef8d2ec8f65b5f9001e512befc095` (2026-08-28)

Files keep their upstream SPDX headers; new files carry a 2026 header. To take an
upstream fix, diff the base against a newer upstream commit and cherry-pick the
hunks that touch files still present here:

```bash
git -C <aiq clone> diff bf4e67d1 <new> -- frontends/ui
```

## What changed and why

The demo has one agent (Hermes, in an OpenShell sandbox) behind a plain FastAPI
API, no app authentication and no uploads. Every question is a durable job on
`/v1/jobs/async`, and its answer is the job's `final_report`. Upstream's NAT
chat, its deep-research panel and everything only they used are removed.

### Deleted

| Area                                                                              | Files                                                                                                                                                                                                                                                                                                                                                                                     |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NAT WebSocket chat and HITL                                                       | `server.js`, `websocket-cookie.js`, `adapters/api/{websocket-client,chat-client,schemas}.ts`, `features/chat/hooks/{use-websocket-chat,use-chat,use-connection-recovery}.ts`, `features/chat/components/AgentPrompt.tsx`, `features/chat/lib/{intermediate-step-parser,transport-auth-signals}.ts`                                                                                        |
| NAT deep-research panel (agents, LLM steps, tool calls, todos, files, report tab) | `features/chat/components/ChatThinking.tsx`, `features/chat/hooks/use-load-job-data.ts`, `features/chat/lib/{deep-research-correlation,deep-research-progress,deep-research-trace,deep-research-todos,prune-message-for-storage}.ts`, `shared/components/research/*`, `shared/components/CollapsibleBlock/*`                                                                              |
| Layout components (21) and copy                                                   | `AgentCard`, `AgentsTab`, `CitationCard`, `DataConnectionsTab`, `DeleteFileConfirmationModal`, `ExportFooter`, `FileCard`, `FileSourceCard`, `FileSourcesTab`, `FilesTab`, `ReportCard`, `ReportTab`, `SettingsPanel`, `SourceCard`, `TaskCard`, `TasksTab`, `ThinkingTab`, `ThoughtCard`, `ThoughtTracesTab`, `ToolCallCard`, `ToolCallsTab`, `research-empty-state-copy.ts`             |
| Documents and uploads                                                             | `features/documents/*`, `adapters/api/{documents-client,documents-schemas}.ts`, `shared/config/file-upload.ts`, `features/chat/components/FileUploadBanner.tsx`, `src/mocks/*` (MSW served only document mocks)                                                                                                                                                                           |
| PDF and Markdown export                                                           | `pages/api/generate-pdf.ts`, `lib/pdf/*`, `hooks/use-download-pdf.ts`, `utils/*`, `shared/utils/artifact-url.ts` (with the `artifact://` image support in `MarkdownRenderer`)                                                                                                                                                                                                             |
| App auth (NextAuth, MCP OAuth)                                                    | `adapters/auth/*`, `app/api/auth/*`, `app/auth/*`, `proxy.ts`, `adapters/api/{authenticated-fetch,mcp-auth-client}.ts`, `shared/utils/rum.ts`                                                                                                                                                                                                                                             |
| Proxies                                                                           | `app/api/chat`, `app/api/generate`, `app/api/generate/respond` (they bypassed the API's allowlist); `app/api/jobs/async` (folded into the allowlisted `/api/v1` proxy)                                                                                                                                                                                                                    |
| Other dead code                                                                   | `adapters/api/config.ts`, `shared/components/{Sources/SourceStrip,Surface/Card}.tsx`, `shared/hooks/use-backend-health.ts`, `shared/lib/humanize.ts`, `storage-logger` pruning loggers, `storage-manager` `getOldestSession` (deprecated) and `calculateChatStoreSize`, `source-utils` `mapCitationSource` (its only caller was the report tab), dead rules in `globals.css` (-632 lines) |
| Build and tooling                                                                 | `deploy/*` (replaced by `Dockerfile`), `.husky`, `tailwind.config.ts` (unused by Tailwind 4)                                                                                                                                                                                                                                                                                                 |

The chat store keeps 27 of its 77 actions. 50 were removed: 12 that upstream
never calls outside the store (of its 14 such actions, `restoreSessionState`
and `persistDeepResearchToSession` are kept), and 38 that only the removed
chat, panel, HITL and upload code used. Three were added: `finishDeepResearch`,
`cancelActiveDeepResearchJob` and `openRecordedSession`.

### Modified

- `features/chat`: `use-deep-research` follows one job's stream and settles the
  answer in the chat (answer-first, as in the prototype); `store` and `types`
  drop the NAT state; the banners and error registry keep only the cases that
  still occur. `DeepResearchBanner` copy follows the prototype ("Run started",
  "Run failed", "Run stopped", "Run unavailable"). The saved sessions' job
  statuses refresh from `app/providers.tsx` in live mode only, not on store
  rehydration, so replay mode never calls the API.
- `features/layout`: `MainLayout` hosts the execution slots and replay mode;
  `ResearchPanel` becomes the "Agent Activity" host, resizable as in the
  prototype (`activity-panel-resize.ts`), following the running job or else the
  conversation's last answer; `ChatArea` adds "View Execution"; `InputArea`
  submits jobs and stops them, and gains the prototype's microphone when
  `SPEECH_INPUT_ENABLED` is set; `DataSourcesPanel` loses
  its Files tab; `AppBar` loses sign-in and gains the Phoenix link;
  `SessionsPanel` gains a read-only mode for recordings.
- `adapters/api`: the job client and data sources client call the same-origin
  `/api/v1` proxy; `pack-client` is new.
- `shared/components/Sources`: references of the form ``Label — evidence `id` —
invocation `id` `` become inspectable evidence (`EvidenceDisclosure`).
- `app`: `layout.tsx` title "Enterprise Research"; `page.tsx` is the landing
  page; the chat moved to `research/page.tsx`.
- Visible copy: the sign-in states and the "AI-Q" label next to the logo are
  gone (as in the prototype); the "Answer complete" banner is gone, since a
  successful job shows its answer instead (answer-first); the no-sources banner
  no longer mentions files;
  the landing page names LangChain instead of LlamaIndex and adds OpenShell and
  Switchyard to the Hermes card, and its analytics card is titled "Market Analytics
  (using CUDA-X Libraries)", with the NVIDIA mark on cuDF, cuGraph and cuML (not the
  RAPIDS logo).
- Landing page: it keeps the prototype's technology logos
  (`public/ecosystem-logos`) and adds the LangChain symbol; Kumo, which has no
  logo file, gets a text badge of the same size. The Market Analytics card
  lists RAPIDS cuDF, cuGraph and cuML. The featured questions sit in a 3 × 2 grid of
  short cards (the full question is in the link and its tooltip), and the
  observability flow shares the footer row, so the page fits 1280×800,
  1440×900 and 1920×1080 without scrolling.

### Added

| File                                                       | Purpose                                                                              |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `app/api/v1/[...path]/route.ts`                            | Allowlisted API proxy (replaces the open upstream proxy)                             |
| `app/api/recordings/[...path]/route.ts`                    | Read-only replay bundle of the active data pack                                      |
| `app/api/health/route.ts`                                  | UI liveness (upstream proxied the backend's health)                                  |
| `shared/config/env.ts`                                     | Runtime configuration: `UI_MODE`, `API_URL`, `PACKS_DIR`, `DATA_PACK`, `PHOENIX_URL` |
| `shared/context/ExecutionFeatureContext.tsx`               | The typed slot where `features/execution` plugs in                                   |
| `features/chat/hooks/use-hermes-chat.ts`                   | Submits a question as a job                                                          |
| `features/landing/*`                                       | Landing page with the pack's featured questions                                      |
| `public/ecosystem-logos/*`                                 | Technology logos on the landing page and the execution graph                         |
| `features/layout/use-recorded-sessions.ts`                 | Recorded sessions in replay mode                                                     |
| `features/speech-input/*`, `adapters/api/speech-client.ts` | The prototype's voice input: record in the browser, transcribe with the API          |
| `Dockerfile`                                               | Standalone image                                                                     |

### Dependencies

- Added: `uuid` (was imported but undeclared).
- Removed: `next-auth`, `@react-pdf/renderer`, `marked`, `http-proxy`,
  `concurrently`, `husky`, `msw`, `@mswjs/data`, `@faker-js/faker`,
  `@types/uuid`, `autoprefixer`.
- Updated: `next` and `eslint-config-next` 16.2.12 → 16.3.7 (image-optimizer
  and `sharp` advisories).
- Kept on the upstream pin: `@nvidia/foundations-react-core` 0.600, React 18.

## Size

Line counts (`wc -l`, excluding `package-lock.json` and the generated `src/generated`):

|                                     | Upstream | Now                                                               | Budget      |
| ----------------------------------- | -------- | ----------------------------------------------------------------- | ----------- |
| TS/TSX/JS                           | 33,046   | 10,184                                                            |             |
| Hand-written CSS                    | 1,432    | 1,719: `globals.css` 804 (upstream, trimmed), landing 915 (added) | < 1k added  |
| Diff vs upstream (without lockfile) |          | +5,780 / −52,969                                                  |             |

The `features/execution` view is not counted here; it has its own budget.

This copy omits the UI's test suites (Vitest specs, Playwright end-to-end and visual tests) and their
dev dependencies.
