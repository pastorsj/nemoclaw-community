// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Server-side runtime configuration.
 *
 * Read at request time so one image serves any environment. Invalid values
 * fail loudly instead of silently falling back.
 *
 * | Variable     | Default               | Meaning                                         |
 * |--------------|-----------------------|-------------------------------------------------|
 * | `UI_MODE`    | `live`                | `live` talks to the API; `replay` never does     |
 * | `API_URL`    | `http://api:8000`     | Base URL of the demo API (server-side only)      |
 * | `PACKS_DIR`  | `/packs`              | Directory holding the data packs                 |
 * | `DATA_PACK`  | `synthetic-market`    | Active pack; recordings are read from its folder |
 * | `PHOENIX_URL`| unset                 | Browser-reachable Phoenix UI; unset hides links  |
 * | `SPEECH_INPUT_ENABLED` | `false`     | Show the microphone (live mode; the API transcribes) |
 * | `SPEECH_INPUT_MAX_SECONDS` | `60`    | Longest recording, 1 to 90 seconds               |
 */

import path from 'node:path'
import type { AppConfig, UiMode } from '@/shared/context'

type Env = Record<string, string | undefined>

const PACK_ID = /^[a-z0-9][a-z0-9-]*$/

const readMode = (env: Env): UiMode => {
  const value = env.UI_MODE?.trim() || 'live'
  if (value !== 'live' && value !== 'replay') {
    throw new Error(`UI_MODE must be "live" or "replay", got "${value}"`)
  }
  return value
}

const readHttpUrl = (name: string, value: string): string => {
  const url = new URL(value)
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`${name} must be an http(s) URL`)
  }
  return value.replace(/\/+$/, '')
}

const readMaxSeconds = (env: Env): number => {
  const raw = env.SPEECH_INPUT_MAX_SECONDS?.trim()
  if (!raw || !/^\d+$/.test(raw)) return 60
  return Math.min(90, Math.max(1, Number(raw)))
}

/** Configuration the browser needs, passed through `AppConfigProvider`. */
export const readAppConfig = (env: Env = process.env): AppConfig => {
  const phoenixUrl = env.PHOENIX_URL?.trim()
  const mode = readMode(env)
  return {
    mode,
    phoenixUrl: phoenixUrl ? readHttpUrl('PHOENIX_URL', phoenixUrl) : null,
    speechInput: {
      // Replay never calls the API, so it has no transcription
      enabled:
        mode === 'live' &&
        ['true', '1', 'yes', 'on'].includes(env.SPEECH_INPUT_ENABLED?.trim().toLowerCase() ?? ''),
      maxSeconds: readMaxSeconds(env),
    },
  }
}

export const readUiMode = (env: Env = process.env): UiMode => readMode(env)

export const readApiUrl = (env: Env = process.env): string =>
  readHttpUrl('API_URL', env.API_URL?.trim() || 'http://api:8000')

/** `$PACKS_DIR/$DATA_PACK/recordings`: the replay bundle of the active data pack. */
export const readRecordingsDir = (env: Env = process.env): string => {
  const pack = env.DATA_PACK?.trim() || 'synthetic-market'
  if (!PACK_ID.test(pack)) {
    throw new Error(`DATA_PACK must match ${PACK_ID}, got "${pack}"`)
  }
  return path.resolve(env.PACKS_DIR?.trim() || '/packs', pack, 'recordings')
}
