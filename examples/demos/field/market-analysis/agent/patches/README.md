# Hermes patches

`agent/Dockerfile` applies these patches to `/opt/hermes` in the official
`nousresearch/hermes-agent:v2026.9.24` image (Hermes 0.21.5, upstream commit
`f97608f1`) with `git apply --exclude='tests/*'`. The image ships no `tests/`
tree, so the upstream test hunks are kept for upstreaming and skipped at build.
The first three touch the Runs API that the job API drives; the fourth, MCP discovery.

| Patch | Why the demo needs it | Upstream | Remove when |
|---|---|---|---|
| `0001-runs-forward-relay-correlation-metadata` | Forwards the run request's `metadata` (job and session references) into the Relay turn, so Phoenix traces join to jobs. | [PR #107708](https://github.com/NousResearch/hermes-agent/pull/107708) covers the other API routes, not `/v1/runs`. | A released Runs API forwards request metadata into the agent turn. |
| `0002-runs-enforce-exact-per-run-toolsets` | Accepts `enabled_toolsets` on `/v1/runs` so each job gets only the tools its selected sources allow, and rejects any widening beyond the profile. | [PR #67837](https://github.com/NousResearch/hermes-agent/pull/67837), closed unmerged. | A released Runs API accepts an explicit, non-widening per-run toolset list. |
| `0003-runs-expose-stable-tool-call-identity` | Adds `tool_call_id` to `tool.started` and `tool.completed` Runs events, which joins graph nodes to receipts. | [PR #53642](https://github.com/NousResearch/hermes-agent/pull/53642), open. | A released Runs API emits the same `tool_call_id` on both events. |
| `0004-mcp-read-readonlyhint-from-mcp-2-annotations` | Hermes read `readOnlyHint` from mcp 2.x tool annotations, which name it `read_only_hint`, so every data tool counted as write-capable. A call cut off when an MCP server restarted (`restart: true` restarts market-analytics when the data changes) then failed instead of being replayed. | Draft ready (not filed). | Hermes reads `read_only_hint` from mcp 2.x annotations. |

The patches apply in order: 0003 edits lines that 0002 adds. When bumping the
Hermes image, rebuild the agent image first; the `git apply` step fails the
build if any patch no longer applies.
