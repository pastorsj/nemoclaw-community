// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * API Proxy Route
 *
 * Forwards `/api/v1/<path>` to `$API_URL/v1/<path>` so the browser only ever
 * talks to this origin and the API stays on the internal network.
 *
 * Only the public surface of the demo API is reachable (see ROUTES); anything
 * else, including the sandbox-only `/internal/**` routes, is a 404. Responses,
 * including Server-Sent Event streams, are passed through unbuffered. A voice
 * recording (`speech/transcriptions`) is forwarded as bytes, up to 3 MiB.
 */

import { readApiUrl, readUiMode } from '@/shared/config/env'

type Method = 'GET' | 'POST'

/** The API routes the UI may call, matched against the path after `/v1/`. */
const ROUTES: ReadonlyArray<readonly [Method, RegExp]> = [
  ['GET', /^pack$/],
  ['GET', /^data_sources(\/.+)?$/],
  ['POST', /^data_sources\/[^/]+\/query$/],
  ['POST', /^jobs\/async\/submit$/],
  ['GET', /^jobs\/async\/job\/[^/]+(\/.+)?$/],
  ['POST', /^jobs\/async\/job\/[^/]+\/cancel$/],
  ['POST', /^jobs\/async\/job\/[^/]+\/benchmark$/],
  ['POST', /^speech\/transcriptions$/],
]

/** A voice recording: 16 kHz mono PCM16 WAV, at most 90 s (about 2.9 MB). */
const SPEECH_PATH = 'speech/transcriptions'
const MAX_SPEECH_BYTES = 3 * 1024 * 1024

/** One URL path segment: no traversal, no encoded separators. */
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9._:-]*$/

/** Request headers forwarded to the API. */
const FORWARDED_REQUEST_HEADERS = ['accept', 'content-type', 'conversation-id', 'last-event-id']

/** Response headers passed back to the browser. */
const FORWARDED_RESPONSE_HEADERS = ['content-type', 'cache-control', 'content-disposition']

const errorResponse = (status: number, code: string, message: string): Response =>
  Response.json({ error: { code, message } }, { status })

/** The API URL for an allowed request, or null. */
const resolveTarget = (method: Method, segments: string[], search: string): string | null => {
  if (!segments.every((segment) => SEGMENT.test(segment))) return null
  const path = segments.join('/')
  if (!ROUTES.some(([allowed, pattern]) => allowed === method && pattern.test(path))) return null
  return `${readApiUrl()}/v1/${path}${search}`
}

/** The request body to forward: text, or a bounded voice recording as bytes. */
const readBody = async (request: Request, speech: boolean): Promise<BodyInit | Response> => {
  if (!speech) return request.text()
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'audio/wav') {
    return errorResponse(415, 'UNSUPPORTED_AUDIO', 'Voice input requires a WAV recording')
  }
  if (Number(request.headers.get('content-length') ?? 0) > MAX_SPEECH_BYTES) {
    return errorResponse(413, 'AUDIO_TOO_LARGE', 'The recording is too large')
  }
  const body = await request.arrayBuffer()
  return body.byteLength > MAX_SPEECH_BYTES
    ? errorResponse(413, 'AUDIO_TOO_LARGE', 'The recording is too large')
    : body
}

const proxy = async (
  request: Request,
  method: Method,
  params: Promise<{ path: string[] }>
): Promise<Response> => {
  if (readUiMode() === 'replay') {
    return errorResponse(404, 'REPLAY_MODE', 'The API is not available in replay mode')
  }

  const segments = (await params).path
  const target = resolveTarget(method, segments, new URL(request.url).search)
  if (!target) return errorResponse(404, 'NOT_FOUND', 'Not found')
  const body =
    method === 'POST' ? await readBody(request, segments.join('/') === SPEECH_PATH) : undefined
  if (body instanceof Response) return body

  const headers = new Headers()
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name)
    if (value) headers.set(name, value)
  }

  let upstream: Response
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      cache: 'no-store',
      // Closing the browser stream closes the upstream SSE connection.
      signal: request.signal,
    })
  } catch {
    return errorResponse(502, 'PROXY_ERROR', 'The API is unavailable')
  }

  const responseHeaders = new Headers()
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name)
    if (value) responseHeaders.set(name, value)
  }
  if (upstream.headers.get('content-type')?.startsWith('text/event-stream')) {
    responseHeaders.set('cache-control', 'no-cache, no-transform')
    responseHeaders.set('x-accel-buffering', 'no')
  }

  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders })
}

interface RouteContext {
  params: Promise<{ path: string[] }>
}

export const GET = (request: Request, { params }: RouteContext): Promise<Response> =>
  proxy(request, 'GET', params)

export const POST = (request: Request, { params }: RouteContext): Promise<Response> =>
  proxy(request, 'POST', params)
