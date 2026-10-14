// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Layout Store
 *
 * Zustand store for managing the main app layout state.
 * Controls sidebar visibility and panel states.
 */

import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import { fetchDataSources, fetchRecordedDataSources } from '@/adapters/api'
import type { LayoutState, LayoutStore } from './types'

const initialState: LayoutState = {
  sessionsCollapsed: false,
  sessionsAutoCollapsed: false,
  rightPanel: 'data-sources',
  execution: null,
  enabledDataSourceIds: [], // Populated when data sources are fetched
  theme: 'system',
  availableDataSources: null,
  dataSourcesLoading: false,
  dataSourcesError: null,
  promptDraft: null,
}

export const useLayoutStore = create<LayoutStore>()(
  devtools(
    (set) => ({
      ...initialState,

      toggleSessionsSidebar: () =>
        set(
          (state) => ({
            sessionsCollapsed: !state.sessionsCollapsed,
            sessionsAutoCollapsed: false,
          }),
          false,
          'toggleSessionsSidebar'
        ),

      setSessionsCollapsed: (collapsed) =>
        set(
          { sessionsCollapsed: collapsed, sessionsAutoCollapsed: false },
          false,
          'setSessionsCollapsed'
        ),

      openRightPanel: (panel) =>
        set(
          (state) => {
            const collapsesSidebar = panel === 'research' || panel === 'data-sources'
            if (collapsesSidebar && !state.sessionsCollapsed) {
              return { rightPanel: panel, sessionsCollapsed: true, sessionsAutoCollapsed: true }
            }
            if (!collapsesSidebar && state.sessionsAutoCollapsed) {
              return { rightPanel: panel, sessionsCollapsed: false, sessionsAutoCollapsed: false }
            }
            return { rightPanel: panel }
          },
          false,
          'openRightPanel'
        ),

      closeRightPanel: () =>
        set(
          (state) =>
            state.sessionsAutoCollapsed
              ? { rightPanel: null, sessionsCollapsed: false, sessionsAutoCollapsed: false }
              : { rightPanel: null },
          false,
          'closeRightPanel'
        ),

      // A new focus object per call, so citing the same source again reopens its node
      openExecution: (jobId, focus) =>
        set({ execution: { jobId, focus: focus ? { ...focus } : null } }, false, 'openExecution'),

      closeExecution: () => set({ execution: null }, false, 'closeExecution'),

      toggleDataSource: (id) =>
        set(
          (state) => {
            const isEnabled = state.enabledDataSourceIds.includes(id)
            return {
              enabledDataSourceIds: isEnabled
                ? state.enabledDataSourceIds.filter((sourceId) => sourceId !== id)
                : [...state.enabledDataSourceIds, id],
            }
          },
          false,
          'toggleDataSource'
        ),

      setEnabledDataSources: (ids) =>
        set({ enabledDataSourceIds: ids }, false, 'setEnabledDataSources'),

      setTheme: (theme) => set({ theme }, false, 'setTheme'),

      setPromptDraft: (value) => set({ promptDraft: value }, false, 'setPromptDraft'),

      fetchDataSources: async (from = 'api') => {
        set({ dataSourcesLoading: true, dataSourcesError: null }, false, 'fetchDataSources/start')

        try {
          const sources =
            from === 'recordings' ? await fetchRecordedDataSources() : await fetchDataSources()
          set(
            {
              availableDataSources: sources,
              enabledDataSourceIds: sources
                .filter((source) => source.default_enabled !== false)
                .map((source) => source.id),
              dataSourcesLoading: false,
            },
            false,
            'fetchDataSources/success'
          )
        } catch (error) {
          set(
            {
              dataSourcesLoading: false,
              dataSourcesError:
                error instanceof Error ? error.message : 'Failed to fetch data sources',
            },
            false,
            'fetchDataSources/error'
          )
        }
      },
    }),
    { name: 'LayoutStore' }
  )
)

/**
 * The active pack's database: its structured source's (a pack has one). A Kumo prediction reads it,
 * though its receipt does not name it.
 */
export const selectPackDatabaseName = (state: LayoutState): string | undefined =>
  state.availableDataSources?.find((source) => source.database_name)?.database_name ?? undefined
