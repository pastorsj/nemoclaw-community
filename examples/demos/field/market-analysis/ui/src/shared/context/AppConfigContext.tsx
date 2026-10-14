// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * App Configuration Context
 *
 * Provides runtime configuration values from server-side environment variables.
 * This allows configuration to be changed at runtime without rebuilding.
 *
 * Values are passed from server components (layout.tsx) to client components
 * via this context, avoiding the need for NEXT_PUBLIC_ prefixed variables.
 */

'use client'

import { createContext, useContext, type ReactNode } from 'react'

/**
 * `live` submits questions to the API. `replay` only shows recorded sessions
 * from the active data pack and never calls the API.
 */
export type UiMode = 'live' | 'replay'

/** Voice input: the composer's microphone, transcribed by the API with NVIDIA Nemotron ASR. */
export interface SpeechInputConfig {
  /** SPEECH_INPUT_ENABLED; live mode only */
  enabled: boolean
  /** The longest recording, in seconds (SPEECH_INPUT_MAX_SECONDS, 1 to 90) */
  maxSeconds: number
}

/**
 * Runtime configuration passed from server to client
 */
export interface AppConfig {
  /** Whether the UI talks to the API or replays recordings (UI_MODE) */
  mode: UiMode
  /** Browser-reachable Phoenix UI (PHOENIX_URL); null hides the Phoenix link */
  phoenixUrl: string | null
  speechInput: SpeechInputConfig
}

const AppConfigContext = createContext<AppConfig | null>(null)

interface AppConfigProviderProps {
  config: AppConfig
  children: ReactNode
}

/**
 * Provider for runtime app configuration.
 * Wrap your app with this provider and pass config from a server component.
 */
export const AppConfigProvider = ({ config, children }: AppConfigProviderProps): ReactNode => {
  return <AppConfigContext.Provider value={config}>{children}</AppConfigContext.Provider>
}

/**
 * Hook to access runtime app configuration.
 * Must be used within an AppConfigProvider.
 *
 * @throws Error if used outside of AppConfigProvider
 */
export const useAppConfig = (): AppConfig => {
  const config = useContext(AppConfigContext)
  if (config === null) {
    throw new Error('useAppConfig must be used within an AppConfigProvider')
  }
  return config
}
