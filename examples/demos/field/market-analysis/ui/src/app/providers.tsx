// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Application Providers
 *
 * Wraps the application with necessary providers:
 * - AppConfigProvider (runtime server-side config)
 * - ExecutionFeatureProvider (the execution view plugged into the base UI)
 * - ThemeProvider (KUI dark/light mode)
 * - live mode: data sources and the recovery of jobs that were running
 */

'use client'

import { type ReactNode, useEffect, useRef, useState } from 'react'
import { ThemeProvider } from '@/adapters/ui'
import {
  AppConfigProvider,
  ExecutionFeatureProvider,
  useAppConfig,
  type AppConfig,
} from '@/shared/context'
import { AppMotionConfig } from '@/shared/lib/motion'
import { useLayoutStore } from '@/features/layout'
import { useChatStore } from '@/features/chat/store'
import { executionFeature } from '@/features/execution'
import type { ThemeMode } from '@/features/layout'

/** There is no sign-in: every browser has one local user for its saved sessions. */
const LOCAL_USER_ID = 'local'

interface ProvidersProps {
  children: ReactNode
  /** Runtime configuration from server-side environment variables */
  config: AppConfig
}

/**
 * Applies theme classes directly to the document element.
 * This ensures theme changes happen without remounting the component tree.
 * Defers application until after hydration to prevent SSR mismatches.
 */
const useThemeEffect = (theme: ThemeMode): void => {
  const [mounted, setMounted] = useState(false)

  // Mark as mounted after first render (client-side only)
  useEffect(() => {
    setMounted(true)
  }, [])

  useEffect(() => {
    // Skip during SSR and initial hydration
    if (!mounted) return

    const root = document.documentElement

    // Remove existing theme classes
    root.classList.remove('nv-light', 'nv-dark')

    if (theme === 'system') {
      // Check system preference
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      root.classList.add(prefersDark ? 'nv-dark' : 'nv-light')

      // Listen for system theme changes
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      const handleChange = (e: MediaQueryListEvent): void => {
        root.classList.remove('nv-light', 'nv-dark')
        root.classList.add(e.matches ? 'nv-dark' : 'nv-light')
      }
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    } else {
      // Apply explicit theme
      root.classList.add(theme === 'dark' ? 'nv-dark' : 'nv-light')
    }
  }, [theme, mounted])
}

/**
 * Hook to fetch data sources on app initialization: from the API in live
 * mode, from the recordings bundle's pack.json in replay mode.
 */
const useDataSourcesInit = (isLive: boolean): void => {
  const fetchDataSources = useLayoutStore((state) => state.fetchDataSources)
  const availableDataSources = useLayoutStore((state) => state.availableDataSources)
  const replayRequested = useRef(false)

  useEffect(() => {
    if (availableDataSources !== null) return
    if (isLive) {
      fetchDataSources()
    } else if (!replayRequested.current) {
      // Once: a bundle without pack.json leaves replay without sources
      replayRequested.current = true
      fetchDataSources('recordings')
    }
  }, [isLive, fetchDataSources, availableDataSources])
}

/**
 * Restores per-session data source toggles after the initial API fetch.
 * On page refresh, fetchDataSources enables the default sources.
 * This hook overrides that default with the stored per-session selection.
 * Waits for both availableDataSources and a hydrated conversation before restoring.
 */
const useDataSourceSessionRestore = (): void => {
  const availableDataSources = useLayoutStore((state) => state.availableDataSources)
  const setEnabledDataSources = useLayoutStore((state) => state.setEnabledDataSources)
  const conversationId = useChatStore((state) => state.currentConversation?.id)
  const restoredRef = useRef(false)

  useEffect(() => {
    if (restoredRef.current || !availableDataSources) return

    const conversation = useChatStore.getState().currentConversation
    if (!conversation) return

    const savedIds = conversation.enabledDataSourceIds
    if (savedIds) {
      const availableIds = new Set(availableDataSources.map((s) => s.id))
      setEnabledDataSources(savedIds.filter((id) => availableIds.has(id)))
    }

    restoredRef.current = true
  }, [availableDataSources, conversationId, setEnabledDataSources])
}

/**
 * Theme wrapper that syncs with layout store.
 * Applies theme classes directly to document for instant updates.
 * Uses defer prop to prevent hydration mismatches.
 */
const ThemeWrapper = ({ children }: { children: ReactNode }): ReactNode => {
  const theme = useLayoutStore((state) => state.theme)
  const isLive = useAppConfig().mode === 'live'

  useThemeEffect(theme)
  useDataSourcesInit(isLive)
  useDataSourceSessionRestore()

  return (
    <ThemeProvider theme={theme} global defer>
      <AppMotionConfig>{children}</AppMotionConfig>
    </ThemeProvider>
  )
}

/**
 * Selects the local user. In replay mode it starts with no conversation and
 * never touches the saved live sessions. In live mode it recovers their jobs:
 * - Settles the saved sessions' jobs that ended while the page was closed.
 * - Reconnects to running/submitted jobs for page refresh recovery.
 * - Settles 'starting' banners of jobs that ended while the page was closed.
 */
const SessionRestorer = ({ children }: { children: ReactNode }): ReactNode => {
  const [mounted, setMounted] = useState(false)
  const isLive = useAppConfig().mode === 'live'
  const setCurrentUser = useChatStore((state) => state.setCurrentUser)
  const startNewSessionDraft = useChatStore((state) => state.startNewSessionDraft)
  const refreshDeepResearchSessionStatuses = useChatStore(
    (state) => state.refreshDeepResearchSessionStatuses
  )
  const reconnectToActiveJob = useChatStore((state) => state.reconnectToActiveJob)
  const cleanupOrphanedStartingBanners = useChatStore(
    (state) => state.cleanupOrphanedStartingBanners
  )
  const currentConversationId = useChatStore((state) => state.currentConversation?.id)
  const isDeepResearchStreaming = useChatStore((state) => state.isDeepResearchStreaming)

  useEffect(() => {
    setMounted(true)
    setCurrentUser(LOCAL_USER_ID)
    if (isLive) void refreshDeepResearchSessionStatuses()
    else startNewSessionDraft()
  }, [isLive, setCurrentUser, startNewSessionDraft, refreshDeepResearchSessionStatuses])

  useEffect(() => {
    if (!isLive || !mounted || !currentConversationId || isDeepResearchStreaming) return

    const restore = async () => {
      await reconnectToActiveJob()
      await cleanupOrphanedStartingBanners()
    }
    restore()
  }, [
    isLive,
    mounted,
    currentConversationId,
    isDeepResearchStreaming,
    reconnectToActiveJob,
    cleanupOrphanedStartingBanners,
  ])

  return <>{children}</>
}

export const Providers = ({ children, config }: ProvidersProps): ReactNode => (
  <AppConfigProvider config={config}>
    <ExecutionFeatureProvider feature={executionFeature}>
      <ThemeWrapper>
        <SessionRestorer>{children}</SessionRestorer>
      </ThemeWrapper>
    </ExecutionFeatureProvider>
  </AppConfigProvider>
)
