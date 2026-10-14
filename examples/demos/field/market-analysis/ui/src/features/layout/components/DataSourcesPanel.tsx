// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * DataSourcesPanel Component
 *
 * Right-side panel for choosing which data sources of the active data pack
 * the agent may use for the next question.
 */

'use client'

import { type FC, memo, useCallback, useEffect, useMemo, useRef } from 'react'
import { Flex, Text, Switch, Button } from '@/adapters/ui'
import { useShallow } from 'zustand/react/shallow'
import { Close, Globe, LoadingSpinner } from '@/adapters/ui/icons'
import { useReducedMotion } from '@/hooks/use-reduced-motion'
import { useLayoutStore } from '../store'
import { useIsCurrentSessionBusy, useChatStore } from '@/features/chat'
import { cn } from '@/shared/lib/cn'
import { DataConnectionCard } from './DataConnectionCard'

/**
 * Panel for managing data sources.
 * Opens from the right side of the screen.
 */
export const DataSourcesPanel: FC = memo(function DataSourcesPanel() {
  const saveDataSourcesToConversation = useChatStore((state) => state.saveDataSourcesToConversation)

  const isOpen = useLayoutStore((s) => s.rightPanel === 'data-sources')
  const { enabledDataSourceIds, availableDataSources, dataSourcesLoading, dataSourcesError } =
    useLayoutStore(
      useShallow((s) => ({
        enabledDataSourceIds: s.enabledDataSourceIds,
        availableDataSources: s.availableDataSources,
        dataSourcesLoading: s.dataSourcesLoading,
        dataSourcesError: s.dataSourcesError,
      }))
    )
  const closeRightPanel = useLayoutStore((s) => s.closeRightPanel)
  const toggleDataSource = useLayoutStore((s) => s.toggleDataSource)
  const setEnabledDataSources = useLayoutStore((s) => s.setEnabledDataSources)
  const fetchDataSources = useLayoutStore((s) => s.fetchDataSources)

  const panelRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)

  // Keep the closed panel out of the tab order and restore focus to its opener.
  useEffect(() => {
    const el = panelRef.current
    if (isOpen) {
      openerRef.current = document.activeElement as HTMLElement | null
      el?.removeAttribute('inert')
      return
    }
    el?.setAttribute('inert', '')
    const opener = openerRef.current
    openerRef.current = null
    opener?.focus?.()
  }, [isOpen])

  // Source changes are disabled while this session has a question in flight
  const isBusy = useIsCurrentSessionBusy()
  const prefersReducedMotion = useReducedMotion()

  const sources = useMemo(() => availableDataSources ?? [], [availableDataSources])
  const enabledSourcesSet = new Set(enabledDataSourceIds)
  const enabledCount = sources.filter((s) => enabledSourcesSet.has(s.id)).length
  // A master on/off switch: on while any source is enabled; clicking turns all off.
  const anyEnabled = enabledCount > 0

  const handleToggle = useCallback(
    (sourceId: string, enabled: boolean) => {
      toggleDataSource(sourceId)
      saveDataSourcesToConversation(
        enabled
          ? [...enabledDataSourceIds, sourceId]
          : enabledDataSourceIds.filter((id) => id !== sourceId)
      )
    },
    [toggleDataSource, enabledDataSourceIds, saveDataSourcesToConversation]
  )

  const handleToggleAll = useCallback(() => {
    const updatedIds = anyEnabled ? [] : sources.map((s) => s.id)
    setEnabledDataSources(updatedIds)
    saveDataSourcesToConversation(updatedIds)
  }, [anyEnabled, sources, setEnabledDataSources, saveDataSourcesToConversation])

  return (
    <div
      ref={panelRef}
      className={cn(
        'border-base bg-surface-base h-full shrink-0 overflow-hidden',
        isOpen && 'border-l'
      )}
      style={{
        width: isOpen ? '400px' : '0px',
        minWidth: isOpen ? '400px' : '0px',
        transition: prefersReducedMotion
          ? 'none'
          : 'width 600ms ease-in-out, min-width 600ms ease-in-out',
      }}
      aria-hidden={!isOpen}
    >
      <Flex
        direction="col"
        className="h-full w-[400px]"
        style={{
          visibility: isOpen ? 'visible' : 'hidden',
          opacity: isOpen ? 1 : 0,
          transition: prefersReducedMotion
            ? 'none'
            : isOpen
              ? 'opacity 100ms ease-in-out, visibility 0ms'
              : 'opacity 100ms ease-in-out 500ms, visibility 0ms 600ms',
        }}
      >
        {/* Header with close affordance */}
        <Flex
          align="center"
          justify="between"
          className="border-base shrink-0 border-b py-4 pl-6 pr-4"
        >
          <Flex align="center" gap="2">
            <Globe className="h-5 w-5" />
            <Text kind="label/semibold/md" className="text-primary">
              Data Sources
            </Text>
          </Flex>
          <Button
            kind="tertiary"
            size="small"
            onClick={closeRightPanel}
            aria-label="Close data sources panel"
            title="Close data sources"
          >
            <Close className="h-4 w-4" aria-hidden="true" />
          </Button>
        </Flex>

        <Flex direction="col" className="flex-1 overflow-y-auto px-6 py-4">
          {/* All Connections Toggle */}
          <Text
            kind="label/semibold/xs"
            className="text-subtle mb-3 font-mono uppercase tracking-[0.08em]"
          >
            All Connections
          </Text>
          <Flex
            align="center"
            justify="between"
            className={cn(
              'surface-card mb-4 border p-3',
              isBusy ? 'border-base opacity-50' : anyEnabled ? 'brand-tint border' : 'border-base'
            )}
            title={isBusy ? 'Data source changes disabled during active operations' : undefined}
          >
            <Text kind="label/semibold/sm" className="text-primary">
              {anyEnabled ? 'Disable Selected' : 'Enable Compatible'}
            </Text>
            <Switch
              size="small"
              checked={anyEnabled}
              onCheckedChange={handleToggleAll}
              disabled={isBusy}
              aria-label={
                isBusy
                  ? 'Toggle all connections (disabled)'
                  : anyEnabled
                    ? 'Disable selected connections'
                    : 'Enable compatible connections'
              }
            />
          </Flex>

          {/* Individual Connections */}
          <Text
            kind="label/semibold/xs"
            className="text-subtle mb-3 font-mono uppercase tracking-[0.08em]"
          >
            Individual Connections ({sources.length})
          </Text>
          <Text kind="body/regular/xs" className="text-subtle mb-3">
            Each pack has one market database plus its document collections. Enable any
            combination; the agent uses only the enabled ones.
          </Text>

          {dataSourcesLoading ? (
            <Flex align="center" justify="center" className="py-8">
              <LoadingSpinner size="medium" aria-label="Loading data sources" />
            </Flex>
          ) : dataSourcesError ? (
            <Flex direction="col" align="center" className="py-4">
              <Text kind="body/regular/sm" className="text-error mb-2">
                Unable to load data sources
              </Text>
              <Text kind="body/regular/xs" className="text-subtle mb-3">
                {dataSourcesError}
              </Text>
              <Button
                kind="secondary"
                size="small"
                onClick={() => fetchDataSources()}
                aria-label="Retry loading data sources"
              >
                Retry
              </Button>
            </Flex>
          ) : sources.length === 0 ? (
            <Flex direction="col" align="center" className="py-4">
              <Text kind="body/regular/sm" className="text-subtle">
                No data sources available
              </Text>
            </Flex>
          ) : (
            <Flex direction="col" gap="2">
              {sources.map((source) => (
                <DataConnectionCard
                  key={source.id}
                  source={source}
                  isEnabled={enabledSourcesSet.has(source.id)}
                  isBusy={isBusy}
                  onToggle={handleToggle}
                />
              ))}
            </Flex>
          )}
        </Flex>

        {/* Footer summary */}
        <Flex direction="col" className="border-base shrink-0 border-t px-6 py-3">
          <Text kind="body/regular/xs" className="text-subtle">
            {enabledCount} of {sources.length} available connections enabled. Enabled connections
            will be available to the AI assistant.
          </Text>
        </Flex>
      </Flex>
    </div>
  )
})
