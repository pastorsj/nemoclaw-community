// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AppBar Component
 *
 * Top navigation bar with the logo (new session), session title, and actions:
 * data sources, the Phoenix trace viewer (when configured; shown disabled in
 * replay, which has no traces), theme, and the Default User avatar (the demo
 * has no sign-in).
 */

'use client'

import { type FC, memo, useCallback, useEffect, useState } from 'react'
import { Flex, Text, Button, Logo, Avatar, Popover, Divider, Tooltip } from '@/adapters/ui'
import { Globe, Info, Moon, OpenExternal, Sun } from '@/adapters/ui/icons'
import { useAppConfig } from '@/shared/context'
import { useLayoutStore } from '../store'
import { cn } from '@/shared/lib/cn'
import type { ThemeMode } from '../types'

interface AppBarProps {
  /** Current session title to display */
  sessionTitle?: string
  /** Callback when a new session is requested */
  onNewSession?: () => void
  /** Accessible copy for the logo action when it changes context */
  newSessionActionLabel?: string
  /** Disable creating a new session while a question is being submitted */
  isNewSessionDisabled?: boolean
  /** Disable the data sources action (replay mode: recorded sessions are read only) */
  isDataSourceSelectionDisabled?: boolean
  /** A recorded session is open: it has no traces in this deployment's Phoenix */
  isRecordedSession?: boolean
}

/**
 * Main navigation bar at the top of the application.
 */
export const AppBar: FC<AppBarProps> = memo(function AppBar({
  sessionTitle = '',
  onNewSession,
  newSessionActionLabel = 'Create new session',
  isNewSessionDisabled = false,
  isDataSourceSelectionDisabled = false,
  isRecordedSession = false,
}) {
  const { mode, phoenixUrl } = useAppConfig()
  const rightPanel = useLayoutStore((s) => s.rightPanel)
  const isDataSourcesOpen = rightPanel === 'data-sources'
  const theme = useLayoutStore((s) => s.theme)
  const setTheme = useLayoutStore((s) => s.setTheme)
  const [isUserMenuOpen, setIsUserMenuOpen] = useState(false)
  const [systemPrefersDark, setSystemPrefersDark] = useState(false)

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    setSystemPrefersDark(mq.matches)
    const handler = (e: MediaQueryListEvent): void => setSystemPrefersDark(e.matches)
    mq.addEventListener?.('change', handler)
    return () => mq.removeEventListener?.('change', handler)
  }, [])

  const isDarkMode = theme === 'dark' || (theme === 'system' && systemPrefersDark)
  const toggleTheme = useCallback(() => {
    setTheme(isDarkMode ? 'light' : 'dark')
  }, [isDarkMode, setTheme])

  const handleAddSourcesClick = useCallback(() => {
    const { rightPanel, closeRightPanel, openRightPanel } = useLayoutStore.getState()
    if (rightPanel === 'data-sources') {
      closeRightPanel()
    } else {
      openRightPanel('data-sources')
    }
  }, [])

  const handleNewSessionClick = useCallback(() => {
    if (isNewSessionDisabled) return
    onNewSession?.()
  }, [isNewSessionDisabled, onNewSession])

  return (
    <header className="border-base border-b">
      <Flex align="center" justify="between" className="h-[var(--header-height)] gap-4 px-4">
        {/* Left section: New session button + session title */}
        <Flex align="center" gap="2" className="min-w-0 flex-1">
          <Button
            kind="tertiary"
            size="small"
            onClick={handleNewSessionClick}
            disabled={isNewSessionDisabled}
            aria-label={newSessionActionLabel}
            title={
              isNewSessionDisabled
                ? 'Cannot create new session while an answer is running'
                : newSessionActionLabel
            }
          >
            <Logo kind="logo-only" size="small" />
          </Button>
          {sessionTitle && (
            <Flex justify="start">
              <Divider orientation="vertical" />
            </Flex>
          )}
          <div className="ml-4 hidden min-w-0 flex-1 items-center md:flex">
            <Text
              kind="body/regular/md"
              className="text-subtle block w-full max-w-[360px] truncate lg:max-w-[480px] xl:max-w-[560px]"
            >
              {sessionTitle}
            </Text>
          </div>
        </Flex>

        {/* Right section: Actions */}
        <Flex align="center" gap="2" className="shrink-0">
          <Button
            kind="tertiary"
            size="small"
            onClick={handleAddSourcesClick}
            disabled={isDataSourceSelectionDisabled}
            aria-label="Add data sources"
            aria-pressed={isDataSourcesOpen}
            title="Add data sources"
            className={cn(isDataSourcesOpen && 'brand-tint')}
          >
            <Flex align="center" gap="1">
              <Globe className="h-4 w-4" />
              <Text kind="label/regular/md">Data Sources</Text>
            </Flex>
          </Button>

          {mode === 'replay' || (isRecordedSession && phoenixUrl) ? (
            <Tooltip
              side="bottom"
              openDelayDuration={200}
              slotContent={
                mode === 'replay'
                  ? 'Traces are only available in live mode.'
                  : 'Recorded sessions have no traces in Phoenix.'
              }
            >
              <span
                role="link"
                aria-disabled="true"
                tabIndex={0}
                aria-label={
                  mode === 'replay'
                    ? 'Phoenix traces are only available in live mode'
                    : 'Phoenix traces are only available for live sessions'
                }
                data-testid="phoenix-observability-link"
                className="text-primary focus-visible:ring-brand inline-flex h-8 cursor-not-allowed items-center gap-1 rounded px-2 focus-visible:outline-none focus-visible:ring-2"
              >
                <OpenExternal className="h-4 w-4" width={16} height={16} />
                <Text kind="label/regular/md">Phoenix</Text>
              </span>
            </Tooltip>
          ) : (
            phoenixUrl && (
              <a
                href={phoenixUrl}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Open Phoenix observability"
                title="Open Phoenix observability"
                data-testid="phoenix-observability-link"
                className="text-primary hover:bg-raised focus-visible:ring-brand inline-flex h-8 cursor-pointer items-center gap-1 rounded px-2 no-underline transition-colors focus-visible:outline-none focus-visible:ring-2"
              >
                <OpenExternal className="h-4 w-4" width={16} height={16} />
                <Text kind="label/regular/md">Phoenix</Text>
              </a>
            )
          )}

          {/* Theme toggle: quick dark/light switch */}
          <Button
            kind="tertiary"
            size="small"
            onClick={toggleTheme}
            aria-label={isDarkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            title={isDarkMode ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {isDarkMode ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>

          {/* User section: the demo has no sign-in, so the Default User notice */}
          <Popover
            open={isUserMenuOpen}
            onOpenChange={setIsUserMenuOpen}
            side="bottom"
            align="end"
            className="bg-surface-base"
            slotContent={<AuthDisabledContent />}
          >
            <Button
              kind="tertiary"
              size="small"
              aria-label="Default User - Authentication Not Configured"
              title="Default User set. Authentication Not Configured."
              className="ml-2"
            >
              <Avatar size="small" fallback="D" />
            </Button>
          </Popover>
        </Flex>
      </Flex>
    </header>
  )
})

const APPEARANCE_SEGMENTS: { mode: ThemeMode; label: string }[] = [
  { mode: 'system', label: 'System' },
  { mode: 'dark', label: 'Dark' },
  { mode: 'light', label: 'Light' },
]

const AppearanceThemeControl: FC = () => {
  const theme = useLayoutStore((s) => s.theme)
  const setTheme = useLayoutStore((s) => s.setTheme)

  return (
    <Flex direction="col" gap="2">
      <Text kind="label/regular/sm" className="text-subtle">
        Appearance
      </Text>
      <Flex
        align="center"
        gap="1"
        className="p-1"
        role="radiogroup"
        aria-label="Theme"
        style={{
          background: 'var(--color-component-track-background, #FFFFFF33)',
          borderRadius: 'var(--radius-lg)',
        }}
      >
        {APPEARANCE_SEGMENTS.map(({ mode, label }) => {
          const selected = theme === mode
          return (
            <Button
              key={mode}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={`${label} theme`}
              kind="tertiary"
              size="small"
              onClick={() => setTheme(mode)}
              className={`h-auto min-h-9 flex-1 rounded-[var(--radius-md)] border-0 px-2 py-1.5 shadow-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--color-border-focus,#76b900)] ${
                selected
                  ? '!bg-black !text-white hover:!bg-black'
                  : 'bg-transparent hover:bg-white/10'
              }`}
            >
              <Flex align="center" justify="center" gap="1" className="w-full">
                {mode === 'dark' ? (
                  <Moon
                    className={`h-4 w-4 shrink-0 ${selected ? '!text-white' : 'text-primary'}`}
                    width={16}
                    height={16}
                  />
                ) : null}
                {mode === 'light' ? (
                  <Sun
                    className={`h-4 w-4 shrink-0 ${selected ? '!text-white' : 'text-primary'}`}
                    width={16}
                    height={16}
                  />
                ) : null}
                <Text
                  kind={selected ? 'label/semibold/sm' : 'label/regular/sm'}
                  className={selected ? 'text-white' : 'text-primary'}
                >
                  {label}
                </Text>
              </Flex>
            </Button>
          )
        })}
      </Flex>
    </Flex>
  )
}

/**
 * Content shown when authentication is disabled
 * Displays info message instead of sign out option
 */
const AuthDisabledContent: FC = () => {
  return (
    <Flex direction="col" gap="3" className="min-w-[240px] p-4">
      {/* User info section */}
      <Flex align="center" gap="3">
        <Avatar size="medium" fallback="D" />
        <Flex direction="col" gap="1">
          <Text kind="label/bold/md" className="text-primary">
            Default User
          </Text>
        </Flex>
      </Flex>

      {/* Info message */}
      <Flex align="center" gap="2" className="border-base rounded border p-3">
        <Info className="h-4 w-4 shrink-0 text-[var(--text-color-subtle)]" />
        <Text kind="body/regular/sm" className="text-subtle">
          Authentication Not Configured
        </Text>
      </Flex>

      <Divider />

      <AppearanceThemeControl />
    </Flex>
  )
}
