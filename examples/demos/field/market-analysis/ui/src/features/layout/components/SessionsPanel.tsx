// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * SessionsPanel Component
 *
 * Persistent left sidebar of session history. Pushes the chat rather than
 * overlaying it, and collapses to a slim icon rail. The top actions (toggle,
 * new session, search) share one fixed-height row in both states, so their
 * icons keep the same size and position when the rail expands. With recorded
 * sessions, "My sessions | Recorded" switches between the browser's sessions
 * and the data pack's read-only recordings, as in the original demo UI.
 */

'use client'

import {
  type FC,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  memo,
  useCallback,
  useMemo,
  useState,
  useRef,
  useEffect,
} from 'react'
import { Flex, Text, Button } from '@/adapters/ui'
import {
  Chat,
  ChatMessage,
  Close,
  DocumentCheckmark,
  Edit,
  Error as ErrorIcon,
  LoadingSpinner,
  Menu,
  Plus,
  Search,
  Trash,
} from '@/adapters/ui/icons'
import { useLayoutStore } from '../store'
import { useChatStore } from '@/features/chat'
import { useReducedMotion } from '@/hooks/use-reduced-motion'
import { checkStorageHealth } from '@/features/chat/lib/storage-manager'
import { cn } from '@/shared/lib/cn'
import type { RecordedSessionSummary } from '@/shared/context'
import { ToolPills, describePills } from '@/shared/components/ToolPills'
import { DeleteSessionConfirmationModal } from './DeleteSessionConfirmationModal'
import { DeleteAllSessionsConfirmationModal } from './DeleteAllSessionsConfirmationModal'

const RAIL_SESSION_LIMIT = 12

interface Session {
  id: string
  title: string
  date: Date
  hasActiveDeepResearch?: boolean
  hasCompletedReport?: boolean
}

/** The data pack's recorded sessions, listed under "Recorded". */
export interface RecordedCollection {
  sessions: RecordedSessionSummary[]
  status: 'loading' | 'ready' | 'error'
  error: string | null
  /** The open recorded session */
  selectedId: string | null
  /** The session being loaded */
  loadingId: string | null
  onSelect: (sessionId: string) => void
  onRetry: () => void
  /** Leave the open recorded session for a live one, when "My sessions" is chosen */
  onLeave?: () => void
}

interface SessionsPanelProps {
  /** List of sessions to display */
  sessions?: Session[]
  /** Currently selected session ID */
  selectedSessionId?: string
  /** Callback when a session is selected */
  onSelectSession?: (sessionId: string) => void
  /** Callback when new session is clicked */
  onNewSession?: () => void
  /** Callback when a session is deleted */
  onDeleteSession?: (sessionId: string) => void
  /** Callback when all sessions are deleted */
  onDeleteAllSessions?: () => void
  /** Callback when a session is renamed */
  onRenameSession?: (sessionId: string, newTitle: string) => void
  /** Recorded sessions, shown in a second collection when given */
  recorded?: RecordedCollection
  /** Replay mode: browse only, no create, rename or delete */
  readOnly?: boolean
}

/**
 * A single top-level nav row (toggle / new / search). Fixed height with a
 * fixed-size icon slot so the icon stays put when the rail expands; the label
 * only appears in the expanded state.
 */
const NavRow: FC<{
  icon: ReactNode
  label: string
  collapsed: boolean
  onClick?: () => void
  disabled?: boolean
  ariaLabel?: string
  title?: string
  buttonRef?: Ref<HTMLButtonElement>
}> = ({ icon, label, collapsed, onClick, disabled = false, ariaLabel, title, buttonRef }) => (
  <button
    ref={buttonRef}
    type="button"
    onClick={onClick}
    disabled={disabled}
    aria-label={ariaLabel ?? label}
    title={title ?? label}
    className={cn(
      'flex h-10 w-full items-center rounded-lg transition-colors',
      collapsed ? 'justify-center px-0' : 'gap-3 px-3',
      disabled ? 'cursor-not-allowed opacity-50' : 'hover:bg-surface-raised-50 cursor-pointer'
    )}
  >
    <span className="text-secondary grid h-5 w-5 shrink-0 place-items-center">{icon}</span>
    {!collapsed && (
      <Text kind="label/regular/sm" className="text-primary truncate">
        {label}
      </Text>
    )}
  </button>
)

/**
 * Sessions sidebar with history grouped by date, collapsible to an icon rail.
 */
export const SessionsPanel: FC<SessionsPanelProps> = memo(function SessionsPanel({
  sessions = [],
  selectedSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onDeleteAllSessions,
  onRenameSession,
  recorded,
  readOnly = false,
}) {
  const collapsed = useLayoutStore((s) => s.sessionsCollapsed)
  const toggleSidebar = useLayoutStore((s) => s.toggleSessionsSidebar)
  const setCollapsed = useLayoutStore((s) => s.setSessionsCollapsed)
  const prefersReducedMotion = useReducedMotion()

  const isSessionBusy = useChatStore((s) => s.isSessionBusy)
  const anySessionBusy = useChatStore((s) => s.hasAnyBusySession())
  const refreshDeepResearchSessionStatuses = useChatStore(
    (s) => s.refreshDeepResearchSessionStatuses
  )
  // Navigation is blocked only while a question is being submitted; jobs run
  // server-side and do NOT block navigation.
  const isNavigationBlocked = useChatStore((s) => s.isStreaming)

  const [searchQuery, setSearchQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const recordedSelectedId = recorded?.selectedId ?? null
  const [sessionCollection, setSessionCollection] = useState<'saved' | 'recorded'>(() =>
    recorded && (readOnly || recordedSelectedId) ? 'recorded' : 'saved'
  )
  const [recordedSearchQuery, setRecordedSearchQuery] = useState('')
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [deleteAllModalOpen, setDeleteAllModalOpen] = useState(false)
  const [sessionToDelete, setSessionToDelete] = useState<string | null>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)
  const refreshStatusesInFlightRef = useRef(false)

  const [storagePercent, setStoragePercent] = useState<number>(0)
  useEffect(() => {
    if (collapsed || readOnly) return
    const { percentUsed } = checkStorageHealth()
    setStoragePercent(Math.round(percentUsed))
    if (!refreshStatusesInFlightRef.current) {
      refreshStatusesInFlightRef.current = true
      void Promise.resolve(refreshDeepResearchSessionStatuses()).finally(() => {
        refreshStatusesInFlightRef.current = false
      })
    }
  }, [collapsed, readOnly, refreshDeepResearchSessionStatuses])

  useEffect(() => {
    if (recordedSelectedId) setSessionCollection('recorded')
  }, [recordedSelectedId])

  const handleDeleteClick = useCallback((sessionId: string) => {
    setSessionToDelete(sessionId)
    setDeleteModalOpen(true)
  }, [])

  const handleConfirmDelete = useCallback(() => {
    if (sessionToDelete) {
      onDeleteSession?.(sessionToDelete)
      setSessionToDelete(null)
    }
  }, [sessionToDelete, onDeleteSession])

  const handleDeleteAllClick = useCallback(() => {
    setDeleteAllModalOpen(true)
  }, [])

  const handleConfirmDeleteAll = useCallback(() => {
    onDeleteAllSessions?.()
  }, [onDeleteAllSessions])

  const handleNewSession = useCallback(() => {
    onNewSession?.()
  }, [onNewSession])

  const handleSessionClick = useCallback(
    (sessionId: string) => {
      onSelectSession?.(sessionId)
    },
    [onSelectSession]
  )

  const focusSearch = useCallback(() => {
    requestAnimationFrame(() => searchInputRef.current?.focus())
  }, [])

  const handleSearchClick = useCallback(() => {
    if (collapsed) setCollapsed(false)
    setSearchOpen(true)
    focusSearch()
  }, [collapsed, setCollapsed, focusSearch])

  const closeSearch = useCallback(() => {
    setSearchOpen(false)
    setSearchQuery('')
    requestAnimationFrame(() => searchTriggerRef.current?.focus())
  }, [])

  const clearSearch = useCallback(() => {
    setSearchQuery('')
    focusSearch()
  }, [focusSearch])

  const namedSessions = useMemo(() => sessions.filter((s) => s.title.trim() !== ''), [sessions])

  const filteredSessions = useMemo(() => {
    if (!searchOpen || !searchQuery.trim()) return namedSessions
    const query = searchQuery.toLowerCase()
    return namedSessions.filter((s) => s.title.toLowerCase().includes(query))
  }, [namedSessions, searchOpen, searchQuery])

  const groupedSessions = useMemo(() => groupSessionsByDate(filteredSessions), [filteredSessions])
  const railSessions = useMemo(() => namedSessions.slice(0, RAIL_SESSION_LIMIT), [namedSessions])
  const hasSessions = namedSessions.length > 0
  const isEmptyState = filteredSessions.length === 0
  const recordedSessions = recorded?.sessions
  const filteredRecordedSessions = useMemo(() => {
    const query = recordedSearchQuery.trim().toLowerCase()
    return (recordedSessions ?? []).filter(
      (session) =>
        !query ||
        [session.title, ...session.questions].some((value) => value.toLowerCase().includes(query))
    )
  }, [recordedSessions, recordedSearchQuery])
  const recordedQuestionCount = useMemo(
    () => (recordedSessions ?? []).reduce((total, session) => total + session.questions.length, 0),
    [recordedSessions]
  )
  const filteredRecordedQuestionCount = useMemo(
    () => filteredRecordedSessions.reduce((total, session) => total + session.questions.length, 0),
    [filteredRecordedSessions]
  )

  return (
    <div
      className="border-base bg-surface-base h-full shrink-0 overflow-hidden border-r"
      style={{
        width: collapsed ? '56px' : '300px',
        minWidth: collapsed ? '56px' : '300px',
        transition: prefersReducedMotion
          ? 'none'
          : 'width 600ms ease-in-out, min-width 600ms ease-in-out',
      }}
      aria-label="Sessions"
    >
      <Flex direction="col" className="h-full gap-1 p-2">
        {/* Header row: title + collapse toggle (or a single expand button when collapsed) */}
        {collapsed ? (
          <Flex align="center" justify="center" className="h-10">
            <button
              type="button"
              onClick={toggleSidebar}
              aria-label="Expand sessions sidebar"
              title="Expand sessions"
              className="hover:bg-surface-raised-50 grid h-10 w-10 cursor-pointer place-items-center rounded-lg"
            >
              <Menu className="text-secondary h-5 w-5" />
            </button>
          </Flex>
        ) : (
          <Flex align="center" gap="3" className="h-10 px-3">
            <button
              type="button"
              onClick={toggleSidebar}
              aria-label="Collapse sessions sidebar"
              title="Collapse sessions"
              className="hover:bg-surface-raised-50 grid h-8 w-8 shrink-0 cursor-pointer place-items-center rounded-md"
            >
              <Menu className="text-secondary h-5 w-5" />
            </button>
            <Text kind="label/semibold/md" className="text-primary flex-1 truncate">
              Sessions
            </Text>
          </Flex>
        )}

        <NavRow
          icon={<Plus className="h-5 w-5" />}
          label="New Session"
          collapsed={collapsed}
          onClick={handleNewSession}
          disabled={readOnly || isNavigationBlocked}
          ariaLabel={
            readOnly
              ? 'Start new session (not available in replay mode)'
              : isNavigationBlocked
                ? 'Start new session (disabled during active operations)'
                : 'Start new session'
          }
          title={
            readOnly
              ? 'Replay mode is read only'
              : isNavigationBlocked
                ? 'Cannot create new session while current session is active'
                : 'Start new session'
          }
        />

        {!collapsed && recorded && (
          <div
            role="tablist"
            aria-label="Session collections"
            className="bg-surface-raised-30 mx-2 grid grid-cols-2 gap-1 rounded-lg p-1"
          >
            <button
              type="button"
              role="tab"
              aria-selected={sessionCollection === 'saved'}
              onClick={() => {
                setSessionCollection('saved')
                // The recording stays read only: back to live, as New Session would be
                if (recordedSelectedId) recorded.onLeave?.()
              }}
              className={cn(
                'text-primary rounded-md px-2 py-1.5 text-xs font-semibold transition-colors',
                sessionCollection === 'saved'
                  ? 'bg-surface-raised shadow-sm'
                  : 'hover:bg-surface-raised-50'
              )}
            >
              My sessions
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sessionCollection === 'recorded'}
              onClick={() => setSessionCollection('recorded')}
              className={cn(
                'text-primary rounded-md px-2 py-1.5 text-xs font-semibold transition-colors',
                sessionCollection === 'recorded'
                  ? 'bg-surface-raised shadow-sm'
                  : 'hover:bg-surface-raised-50'
              )}
            >
              Recorded {recorded.sessions.length ? `(${recorded.sessions.length})` : ''}
            </button>
          </div>
        )}

        {sessionCollection === 'saved' &&
          hasSessions &&
          (!collapsed && searchOpen ? (
            <Flex align="center" gap="3" className="h-10 px-3">
              <span className="text-secondary grid h-5 w-5 shrink-0 place-items-center">
                <Search className="h-5 w-5" />
              </span>
              <input
                ref={searchInputRef}
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') closeSearch()
                }}
                onBlur={() => {
                  if (!searchQuery.trim()) closeSearch()
                }}
                placeholder="Search sessions..."
                className="text-primary placeholder:text-subtle h-full min-w-0 flex-1 border-0 bg-transparent text-sm outline-none"
                aria-label="Search sessions"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={clearSearch}
                  aria-label="Clear search"
                  title="Clear search"
                  className="text-subtle hover:text-primary grid h-5 w-5 shrink-0 cursor-pointer place-items-center"
                >
                  <Close className="h-4 w-4" />
                </button>
              )}
            </Flex>
          ) : (
            <NavRow
              icon={<Search className="h-5 w-5" />}
              label="Search sessions"
              collapsed={collapsed}
              onClick={handleSearchClick}
              ariaLabel="Search sessions"
              buttonRef={searchTriggerRef}
            />
          ))}

        {collapsed ? (
          <Flex
            direction="col"
            align="center"
            gap="1"
            className="scrollbar-hide mt-1 w-full flex-1 overflow-y-auto"
          >
            {railSessions.map((session) => {
              const isSelected = selectedSessionId === session.id
              return (
                <button
                  key={session.id}
                  type="button"
                  onClick={() => handleSessionClick(session.id)}
                  disabled={isNavigationBlocked}
                  title={session.title}
                  aria-label={`Session: ${session.title}`}
                  className={cn(
                    'focus-visible:ring-brand grid h-10 w-10 shrink-0 place-items-center rounded-lg outline-none focus-visible:ring-2',
                    isNavigationBlocked ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
                    isSelected ? 'brand-tint' : 'hover:bg-surface-raised-50'
                  )}
                >
                  <SessionStatusIcon
                    session={session}
                    isSessionActive={isSessionBusy(session.id)}
                  />
                </button>
              )
            })}
          </Flex>
        ) : sessionCollection === 'recorded' && recorded ? (
          <RecordedSessionsList
            sessions={filteredRecordedSessions}
            totalCount={recorded.sessions.length}
            questionCount={filteredRecordedQuestionCount}
            totalQuestionCount={recordedQuestionCount}
            status={recorded.status}
            error={recorded.error}
            searchQuery={recordedSearchQuery}
            selectedSessionId={recordedSelectedId}
            loadingSessionId={recorded.loadingId}
            isNavigationBlocked={isNavigationBlocked}
            onSearchQueryChange={setRecordedSearchQuery}
            onSelect={recorded.onSelect}
            onRetry={recorded.onRetry}
          />
        ) : (
          <Flex direction="col" className="scrollbar-hide -mr-1 mt-1 flex-1 overflow-y-auto pr-1">
            {groupedSessions.map((group) => (
              <Flex key={group.label} direction="col" gap="1" className="mb-4">
                <Text
                  kind="label/semibold/xs"
                  className="text-subtle mb-1 px-3 font-mono uppercase tracking-[0.08em]"
                >
                  {group.label}
                </Text>
                {group.sessions.map((session) => (
                  <SessionItem
                    key={session.id}
                    session={session}
                    isSelected={selectedSessionId === session.id}
                    isBusy={isNavigationBlocked}
                    isSessionActive={isSessionBusy(session.id)}
                    onSelect={handleSessionClick}
                    onDelete={readOnly ? undefined : handleDeleteClick}
                    onRename={readOnly ? undefined : onRenameSession}
                  />
                ))}
              </Flex>
            ))}

            {isEmptyState && (
              <Flex
                direction="col"
                align="center"
                justify="center"
                gap="3"
                className="flex-1 px-6 py-12 text-center"
              >
                <span className="brand-chip grid h-12 w-12 place-items-center rounded-full">
                  <ChatMessage className="h-6 w-6" />
                </span>
                <Flex direction="col" gap="1">
                  <Text kind="label/semibold/md" className="text-primary">
                    {searchOpen && searchQuery.trim() ? 'No matching sessions' : 'No sessions yet'}
                  </Text>
                  <Text kind="body/regular/sm" className="text-subtle">
                    {searchOpen && searchQuery.trim()
                      ? 'Try a different search term.'
                      : readOnly
                        ? 'Replay mode shows the recorded sessions only.'
                        : 'Your research sessions will show up here.'}
                  </Text>
                </Flex>
                {!readOnly && !(searchOpen && searchQuery.trim()) && (
                  <Button kind="secondary" size="small" onClick={handleNewSession} className="mt-1">
                    <Flex align="center" gap="2">
                      <Plus className="h-4 w-4" />
                      <Text kind="label/bold/sm">Start a new session</Text>
                    </Flex>
                  </Button>
                )}
              </Flex>
            )}
          </Flex>
        )}

        {!collapsed && !readOnly && sessionCollection === 'saved' && (
          <Flex direction="col" gap="2" className="border-base mt-2 border-t pt-3">
            {hasSessions && (
              <Flex justify="end">
                <Button
                  kind="tertiary"
                  size="tiny"
                  color="danger"
                  onClick={handleDeleteAllClick}
                  disabled={anySessionBusy}
                  aria-label={
                    anySessionBusy ? 'Delete all sessions (disabled)' : 'Delete all sessions'
                  }
                  title={
                    anySessionBusy
                      ? 'Cannot delete while operations are in progress'
                      : 'Delete all sessions'
                  }
                >
                  <Flex align="center" gap="1">
                    <Trash className="h-3.5 w-3.5" />
                    <Text kind="label/regular/xs">Clear all</Text>
                  </Flex>
                </Button>
              </Flex>
            )}
            <Flex align="center" gap="2">
              <div className="bg-surface-raised h-1 flex-1 overflow-hidden rounded-full">
                <div
                  className="h-full rounded-full bg-[color:var(--color-brand)] transition-[width] duration-500 ease-[var(--ease-premium)]"
                  style={{ width: `${Math.min(storagePercent, 100)}%` }}
                />
              </div>
              <Text kind="body/regular/xs" className="text-subtle shrink-0">
                {storagePercent}% used
              </Text>
            </Flex>
            <Text kind="body/regular/xs" className="text-subtle">
              Note: Chat sessions are saved in this browser. Research reports may expire on the
              server.
            </Text>
          </Flex>
        )}
      </Flex>

      <DeleteSessionConfirmationModal
        open={deleteModalOpen}
        onOpenChange={setDeleteModalOpen}
        onConfirm={handleConfirmDelete}
      />

      <DeleteAllSessionsConfirmationModal
        open={deleteAllModalOpen}
        onOpenChange={setDeleteAllModalOpen}
        onConfirm={handleConfirmDeleteAll}
      />
    </div>
  )
})

interface RecordedSessionsListProps {
  sessions: RecordedSessionSummary[]
  totalCount: number
  questionCount: number
  totalQuestionCount: number
  status: RecordedCollection['status']
  error: string | null
  searchQuery: string
  selectedSessionId: string | null
  loadingSessionId: string | null
  isNavigationBlocked: boolean
  onSearchQueryChange: (value: string) => void
  onSelect: (sessionId: string) => void
  onRetry: () => void
  /** Leave the open recorded session for a live one, when "My sessions" is chosen */
  onLeave?: () => void
}

/** The recorded sessions: a search, the count, one row per session, and a note on how they load. */
const RecordedSessionsList: FC<RecordedSessionsListProps> = ({
  sessions,
  totalCount,
  questionCount,
  totalQuestionCount,
  status,
  error,
  searchQuery,
  selectedSessionId,
  loadingSessionId,
  isNavigationBlocked,
  onSearchQueryChange,
  onSelect,
  onRetry,
}) => (
  <Flex direction="col" className="min-h-0 flex-1 pt-1">
    <Flex direction="col" gap="2" className="border-base border-b px-2 pb-3 pt-1">
      <div className="border-base bg-surface-raised-30 flex h-9 items-center gap-2 rounded-lg border px-2.5">
        <Search className="text-subtle h-4 w-4 shrink-0" />
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => onSearchQueryChange(event.target.value)}
          placeholder="Search questions or datasets"
          aria-label="Search recorded sessions"
          className="text-primary placeholder:text-subtle min-w-0 flex-1 border-0 bg-transparent text-xs outline-none"
        />
        {searchQuery && (
          <button
            type="button"
            onClick={() => onSearchQueryChange('')}
            aria-label="Clear recorded session search"
            className="text-subtle hover:text-primary grid h-5 w-5 place-items-center"
          >
            <Close className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {status === 'ready' && (
        <Text kind="body/regular/xs" className="text-subtle px-0.5">
          Showing {sessions.length} of {totalCount} sessions · {questionCount} of{' '}
          {totalQuestionCount} questions
        </Text>
      )}
      {status === 'ready' && error && (
        <Text kind="body/regular/xs" className="text-error px-0.5" role="alert">
          {error}
        </Text>
      )}
    </Flex>

    {status === 'loading' ? (
      <Flex direction="col" align="center" justify="center" gap="2" className="flex-1 p-6">
        <LoadingSpinner className="text-accent-primary" aria-label="Loading recorded sessions" />
        <Text kind="body/regular/sm" className="text-subtle">
          Loading recorded sessions…
        </Text>
      </Flex>
    ) : status === 'error' ? (
      <Flex
        direction="col"
        align="center"
        justify="center"
        gap="3"
        className="flex-1 p-6 text-center"
      >
        <ErrorIcon className="text-error h-6 w-6" />
        <Text kind="body/regular/sm" className="text-secondary">
          {error || 'Unable to load recorded sessions.'}
        </Text>
        <Button kind="secondary" size="small" onClick={onRetry}>
          Retry
        </Button>
      </Flex>
    ) : sessions.length === 0 ? (
      <Flex
        direction="col"
        align="center"
        justify="center"
        gap="2"
        className="flex-1 p-6 text-center"
      >
        <Search className="text-subtle h-6 w-6" />
        <Text kind="label/semibold/sm" className="text-primary">
          No matching recorded sessions
        </Text>
        <Text kind="body/regular/xs" className="text-subtle">
          Try using a broader search.
        </Text>
      </Flex>
    ) : (
      <div className="scrollbar-hide min-h-0 flex-1 overflow-y-auto px-1 py-2">
        {sessions.map((session) => {
          const isSelected = selectedSessionId === session.id
          const isLoading = loadingSessionId === session.id
          const turnCount = session.questions.length
          const turns = `${turnCount} ${turnCount === 1 ? 'turn' : 'turns'}`
          const tools = describePills(session.tools)
          // The label names the session; what the row shows besides, pills included, is its description
          const descriptionId = `recorded-session-${session.id}-description`
          return (
            <button
              key={session.id}
              type="button"
              onClick={() => onSelect(session.id)}
              disabled={isNavigationBlocked || isLoading}
              aria-label={`Recorded session: ${session.title}; Completed`}
              aria-describedby={descriptionId}
              className={cn(
                'focus-visible:ring-brand mb-1.5 flex w-full gap-2 rounded-lg px-2.5 py-2.5 text-left outline-none focus-visible:ring-2',
                isNavigationBlocked || isLoading
                  ? 'cursor-not-allowed opacity-60'
                  : 'hover:bg-surface-raised-50 cursor-pointer',
                isSelected && 'brand-tint bg-surface-raised'
              )}
            >
              <span className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center">
                {isLoading ? (
                  <LoadingSpinner className="text-accent-primary" aria-label="Loading session" />
                ) : (
                  <DocumentCheckmark className="text-success h-4 w-4" aria-label="Completed" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <Text
                  kind="body/regular/sm"
                  className="text-primary line-clamp-3 leading-5"
                  title={session.title}
                >
                  {session.title}
                </Text>
                <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <Text kind="body/regular/xs" className="text-subtle">
                    {turns}
                  </Text>
                  <Text kind="body/regular/xs" className="text-subtle">
                    Completed
                  </Text>
                </span>
                <ToolPills pills={session.tools} className="mt-1.5" />
              </span>
              <span id={descriptionId} className="sr-only">
                {tools ? `${turns}. Tools: ${tools}.` : `${turns}.`}
              </span>
            </button>
          )
        })}
      </div>
    )}

    <Text kind="body/regular/xs" className="border-base text-subtle border-t px-3 py-3">
      Read-only test runs. Answers load on selection; execution data loads only when requested.
    </Text>
  </Flex>
)

/**
 * SessionItem Component
 *
 * Single-line session row (title + status icon) with hover-reveal rename/delete
 * actions and inline rename.
 */
interface SessionItemProps {
  session: Session
  isSelected: boolean
  /** Navigation block: true while a question is being submitted */
  isBusy?: boolean
  /** Per-session block: true when this specific session has a running job */
  isSessionActive?: boolean
  onSelect?: (sessionId: string) => void
  onDelete?: (sessionId: string) => void
  onRename?: (sessionId: string, newTitle: string) => void
}

const SessionItem: FC<SessionItemProps> = ({
  session,
  isSelected,
  isBusy = false,
  isSessionActive = false,
  onSelect,
  onDelete,
  onRename,
}) => {
  const [isHovered, setIsHovered] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [editValue, setEditValue] = useState(session.title)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [isEditing])

  const handleClick = useCallback(() => {
    if (!isEditing && !isBusy) {
      onSelect?.(session.id)
    }
  }, [isEditing, isBusy, onSelect, session.id])

  const handleEditClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      setEditValue(session.title)
      setIsEditing(true)
    },
    [session.title]
  )

  const handleDeleteClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      onDelete?.(session.id)
    },
    [onDelete, session.id]
  )

  const handleSaveRename = useCallback(() => {
    const trimmedValue = editValue.trim()
    if (!trimmedValue) {
      setEditValue(session.title)
      setIsEditing(false)
      return
    }
    if (trimmedValue !== session.title) {
      onRename?.(session.id, trimmedValue)
    }
    setIsEditing(false)
  }, [editValue, session.id, session.title, onRename])

  const handleCancelRename = useCallback(() => {
    setEditValue(session.title)
    setIsEditing(false)
  }, [session.title])

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        handleSaveRename()
      } else if (e.key === 'Escape') {
        e.preventDefault()
        handleCancelRename()
      }
    },
    [handleSaveRename, handleCancelRename]
  )

  const handleInputBlur = useCallback(() => {
    handleSaveRename()
  }, [handleSaveRename])

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setEditValue(e.target.value)
  }, [])

  return (
    <div
      role="button"
      tabIndex={isBusy ? -1 : 0}
      onClick={handleClick}
      onKeyDown={(e) => e.key === 'Enter' && !isEditing && !isBusy && handleClick()}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      className={cn(
        'surface-card focus-visible:ring-brand group flex h-9 w-full items-center gap-2 rounded-lg px-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset',
        isBusy ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
        isSelected ? 'bg-surface-raised brand-tint' : 'hover:bg-surface-raised-50 bg-transparent'
      )}
      aria-label={
        isBusy ? `Session: ${session.title} (processing in progress)` : `Session: ${session.title}`
      }
      aria-disabled={isBusy}
    >
      {isEditing ? (
        <input
          ref={inputRef}
          type="text"
          value={editValue}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onBlur={handleInputBlur}
          onClick={(e) => e.stopPropagation()}
          className="bg-surface-base border-accent-primary text-primary h-7 min-w-0 flex-1 rounded border px-2 py-1 text-sm outline-none"
          aria-label="Edit session title"
        />
      ) : (
        <>
          <span className="grid h-4 w-4 shrink-0 place-items-center">
            <SessionStatusIcon session={session} isSessionActive={isSessionActive} />
          </span>

          <Text
            kind="body/regular/sm"
            title={session.title}
            className="text-primary min-w-0 flex-1 truncate"
          >
            {session.title}
          </Text>

          {/* Hover-reveal rename / delete actions */}
          {isHovered && onDelete && (
            <Flex align="center" gap="1" className="shrink-0">
              <Button
                kind="tertiary"
                size="tiny"
                onClick={handleEditClick}
                disabled={isBusy || isSessionActive}
                aria-label={
                  isBusy || isSessionActive ? 'Rename session (disabled)' : 'Rename session'
                }
                title={
                  isBusy || isSessionActive
                    ? 'Cannot rename while operations are in progress'
                    : 'Rename session'
                }
              >
                <Edit height={16} width={16} />
              </Button>
              <Button
                kind="tertiary"
                size="tiny"
                color="danger"
                onClick={handleDeleteClick}
                disabled={isBusy || isSessionActive}
                aria-label={
                  isBusy || isSessionActive ? 'Delete session (disabled)' : 'Delete session'
                }
                title={
                  isBusy || isSessionActive
                    ? 'Cannot delete while operations are in progress'
                    : 'Delete session'
                }
              >
                <Trash height={16} width={16} />
              </Button>
            </Flex>
          )}
        </>
      )}
    </div>
  )
}

const SessionStatusIcon: FC<{ session: Session; isSessionActive: boolean }> = ({
  session,
  isSessionActive,
}) => {
  const isActive = isSessionActive || session.hasActiveDeepResearch

  if (isActive) {
    return <LoadingSpinner className="text-accent-primary shrink-0" aria-label="Session active" />
  }

  if (session.hasCompletedReport) {
    return (
      <DocumentCheckmark className="text-success h-4 w-4 shrink-0" aria-label="Report completed" />
    )
  }

  return <Chat className="text-subtle h-4 w-4 shrink-0" aria-label="Chat session" />
}

interface SessionGroup {
  label: string
  sessions: Session[]
}

const DAY_MS = 86_400_000

const startOfDay = (date: Date): number => {
  const d = new Date(date)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

/**
 * Buckets a session date into a relative label: Today, Yesterday, Previous 7
 * Days, Previous 30 Days, then by month (with year when not the current one).
 */
const bucketLabel = (date: Date, now: Date): string => {
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS)
  if (diffDays <= 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return 'Previous 7 Days'
  if (diffDays < 30) return 'Previous 30 Days'
  return date.getFullYear() === now.getFullYear()
    ? date.toLocaleDateString('en-US', { month: 'long' })
    : date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

/**
 * Groups sessions into ordered relative-date buckets, preserving the caller's
 * order within and across buckets (the caller sorts by session recency).
 */
const groupSessionsByDate = (sessions: Session[]): SessionGroup[] => {
  const now = new Date()
  const groups = new Map<string, Session[]>()
  for (const session of sessions) {
    const label = bucketLabel(new Date(session.date), now)
    const bucket = groups.get(label)
    if (bucket) {
      bucket.push(session)
    } else {
      groups.set(label, [session])
    }
  }
  return Array.from(groups, ([label, items]) => ({ label, sessions: items }))
}
