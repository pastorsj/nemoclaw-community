// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Layout Feature Types
 *
 * Type definitions for the main app layout including sidebars and panels.
 */

import type { DataSourceFromAPI } from '@/adapters/api'
import type { ExecutionFocus } from '@/shared/context'

/** Theme mode options */
export type ThemeMode = 'light' | 'dark' | 'system'

/** Panels that can be opened on the right side */
export type RightPanelType = 'research' | 'data-sources' | null

/** Layout state for managing panels */
export interface LayoutState {
  /** Whether the sessions sidebar is collapsed */
  sessionsCollapsed: boolean
  /** True when the sidebar was auto-collapsed by opening a right panel (so it can auto-restore) */
  sessionsAutoCollapsed: boolean
  /** Currently open right panel (null = closed) */
  rightPanel: RightPanelType
  /** The run shown in the execution workspace (null = closed) */
  execution: { jobId: string; focus: ExecutionFocus | null } | null
  /** IDs of enabled data sources (array for zustand serialization) */
  enabledDataSourceIds: string[]
  /** Current theme mode */
  theme: ThemeMode
  /** Dynamic data sources from API (null = not loaded yet) */
  availableDataSources: DataSourceFromAPI[] | null
  /** Whether data sources are being fetched */
  dataSourcesLoading: boolean
  /** Error message if data sources fetch failed */
  dataSourcesError: string | null
  /** Text to place in the composer (e.g. a featured question), consumed once */
  promptDraft: string | null
}

/** Layout actions for state management */
export interface LayoutActions {
  /** Toggle the sessions sidebar collapsed state (clears auto-collapse) */
  toggleSessionsSidebar: () => void
  /** Set the sessions sidebar collapsed state (clears auto-collapse) */
  setSessionsCollapsed: (collapsed: boolean) => void
  /** Open a specific right panel (closes any existing) */
  openRightPanel: (panel: RightPanelType) => void
  /** Close the right panel */
  closeRightPanel: () => void
  /** Show a run in the execution workspace, optionally focused on cited evidence */
  openExecution: (jobId: string, focus?: ExecutionFocus) => void
  /** Return from the execution workspace to the conversation */
  closeExecution: () => void
  /** Toggle a data source enabled/disabled by ID */
  toggleDataSource: (id: string) => void
  /** Set all enabled data sources */
  setEnabledDataSources: (ids: string[]) => void
  /** Set the theme mode */
  setTheme: (theme: ThemeMode) => void
  /** Set the composer draft (null clears it) */
  setPromptDraft: (value: string | null) => void
  /**
   * Fetch data sources and enable the ones enabled by default: from the API,
   * or in replay mode from the recordings bundle
   */
  fetchDataSources: (from?: 'api' | 'recordings') => Promise<void>
}

/** Combined layout store type */
export type LayoutStore = LayoutState & LayoutActions
