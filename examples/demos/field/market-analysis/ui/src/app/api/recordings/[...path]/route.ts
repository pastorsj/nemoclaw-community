// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Recordings Route
 *
 * Serves the active data pack's replay bundle, read-only, from
 * `$PACKS_DIR/$DATA_PACK/recordings` (e.g. `/api/recordings/index.json`).
 * Only JSON and JSON Lines files inside that directory are served.
 *
 * `index.json` from a bundle recorded before the index listed each session's
 * `tools` gets them derived from the sessions' recorded events.
 */

import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import { sessionPills, type ToolPillUse } from '@/shared/components/ToolPills'
import { readRecordingsDir } from '@/shared/config/env'

const CONTENT_TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.jsonl': 'application/x-ndjson',
}

/** One path segment: no traversal, no hidden files. */
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9._-]*$/

const notFound = (): Response =>
  Response.json({ error: { code: 'NOT_FOUND', message: 'Not found' } }, { status: 404 })

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const segments = (await params).path
  const contentType = CONTENT_TYPES[path.extname(segments.at(-1) ?? '')]
  if (!contentType || !segments.every((segment) => SEGMENT.test(segment))) return notFound()

  try {
    // Resolve symlinks on both sides so a link cannot escape the bundle.
    const root = await realpath(readRecordingsDir())
    const file = await realpath(path.join(root, ...segments))
    if (!file.startsWith(root + path.sep)) return notFound()
    const body =
      file === path.join(root, 'index.json') ? await withTools(root, file) : await readFile(file)
    return new Response(body, {
      headers: { 'content-type': contentType, 'cache-control': 'no-cache' },
    })
  } catch {
    return notFound()
  }
}

/** Derived pills by session file, kept while the file is unchanged. */
const derived = new Map<string, { modified: number; tools: ToolPillUse[] }>()

/** The index, with `tools` derived for every session that lacks them. */
const withTools = async (root: string, file: string): Promise<string> => {
  const index = JSON.parse(await readFile(file, 'utf8')) as { sessions?: unknown }
  if (!Array.isArray(index.sessions)) return JSON.stringify(index)
  for (const session of index.sessions as Array<Record<string, unknown>>) {
    if (Array.isArray(session.tools) || typeof session.id !== 'string' || !SEGMENT.test(session.id))
      continue
    const sessionFile = path.join(root, 'sessions', `${session.id}.json`)
    try {
      const modified = (await stat(sessionFile)).mtimeMs
      let cached = derived.get(sessionFile)
      if (cached?.modified !== modified) {
        const { turns } = JSON.parse(await readFile(sessionFile, 'utf8')) as { turns?: unknown }
        cached = { modified, tools: sessionPills(Array.isArray(turns) ? turns : []) }
        derived.set(sessionFile, cached)
      }
      session.tools = cached.tools
    } catch {
      session.tools = []
    }
  }
  return JSON.stringify(index)
}
