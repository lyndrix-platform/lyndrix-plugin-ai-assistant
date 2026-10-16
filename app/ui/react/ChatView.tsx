import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { assistantApi, isRunnerUnavailable } from './lib/api'
import type { AssistantStatus, SessionOut } from './lib/api'
import { fmtCost } from './lib/format'
import { runnerIsUp } from './lib/status'
import {
  Badge, Button, EmptyState, ErrorBox, Icon, IconButton, InfoBox, LinkButton, Modal, Spinner,
  goSettings, useAccess, useMediaQuery,
} from './shared'
import Composer from './chat/Composer'
import SessionList, { SessionPicker, sessionTone, useStatusLabel } from './chat/SessionList'
import Transcript from './chat/Transcript'
import { STREAM_LOST, useChatSession } from './chat/useChatSession'

const LAST_SESSION_KEY = 'lyndrix_ai_last_session'
const SESSIONS_POLL_MS = 15_000
const STATUS_POLL_MS = 30_000

/** /ai/chat and the floating "chat" panel (compact). */
export default function ChatView({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation('ai_assistant')
  const access = useAccess()
  const narrow = useMediaQuery('(max-width: 820px)')
  const pickerMode = compact || narrow
  const statusLabel = useStatusLabel()

  const [status, setStatus] = useState<AssistantStatus | null>(null)
  const [sessions, setSessions] = useState<SessionOut[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const [creating, setCreating] = useState(false)
  const [closing, setClosing] = useState<SessionOut | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    try { return localStorage.getItem(LAST_SESSION_KEY) } catch { return null }
  })

  const selectSession = useCallback((id: string | null) => {
    setSelectedId(id)
    try { id ? localStorage.setItem(LAST_SESSION_KEY, id) : localStorage.removeItem(LAST_SESSION_KEY) } catch { /* ignore */ }
  }, [])

  // Keep the list row of the open session in step with the live state.
  const onSessionChanged = useCallback((s: SessionOut) => {
    setSessions((prev) => (prev.some((x) => x.id === s.id) ? prev.map((x) => (x.id === s.id ? { ...x, ...s } : x)) : prev))
  }, [])

  const chat = useChatSession(selectedId, onSessionChanged)

  // ── status + session list polling ──────────────────────────────────────────
  const loadStatus = useCallback(async () => {
    try { setStatus(await assistantApi.status()) } catch { /* shown through the chat errors */ }
  }, [])

  const loadSessions = useCallback(async (quiet = false) => {
    if (!quiet) setSessionsLoading(true)
    try {
      const r = await assistantApi.listSessions({ all: showAll && access.isAdmin, archived: showArchived })
      setSessions(r.sessions)
      setListError(null)
    } catch (err) {
      setListError((err as Error).message)
    } finally {
      setSessionsLoading(false)
    }
  }, [showAll, showArchived, access.isAdmin])

  useEffect(() => { void loadStatus() }, [loadStatus])
  useEffect(() => { void loadSessions() }, [loadSessions])

  const anyRunning = sessions.some((s) => s.status === 'running' || s.status === 'waiting_approval')
  useEffect(() => {
    const ms = anyRunning ? 5000 : SESSIONS_POLL_MS
    const id = window.setInterval(() => { if (!document.hidden) void loadSessions(true) }, ms)
    return () => window.clearInterval(id)
  }, [loadSessions, anyRunning])

  const runnerUp = runnerIsUp(status)
  useEffect(() => {
    const ms = runnerUp && !chat.runnerUnavailable ? STATUS_POLL_MS : 8000
    const id = window.setInterval(() => { if (!document.hidden) void loadStatus() }, ms)
    return () => window.clearInterval(id)
  }, [loadStatus, runnerUp, chat.runnerUnavailable])

  // Drop a remembered session that no longer exists in the current list view.
  useEffect(() => {
    if (sessionsLoading) return
    if (selectedId && !showArchived && !showAll && sessions.length > 0 && !sessions.some((s) => s.id === selectedId) && chat.error) {
      selectSession(null)
    }
  }, [sessions, sessionsLoading, selectedId, showArchived, showAll, chat.error, selectSession])

  // ── actions ────────────────────────────────────────────────────────────────
  const createSession = async () => {
    setCreating(true)
    try {
      const s = await assistantApi.createSession({})
      setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)])
      selectSession(s.id)
      setListError(null)
    } catch (err) {
      setListError(isRunnerUnavailable(err) ? t('chat.runnerUnavailable', { defaultValue: 'The assistant runner is not available right now.' }) : (err as Error).message)
      void loadStatus()
    } finally {
      setCreating(false)
    }
  }

  const closeSession = async (s: SessionOut) => {
    try {
      await assistantApi.closeSession(s.id)
      setClosing(null)
      if (selectedId === s.id) selectSession(null)
      await loadSessions(true)
    } catch (err) {
      setClosing(null)
      setListError((err as Error).message)
    }
  }

  // ── derived ────────────────────────────────────────────────────────────────
  const authNone = status?.auth_mode === 'none'
  const canChat = access.canWrite && !authNone && (runnerUp || !status)
  const current = chat.session ?? sessions.find((s) => s.id === selectedId) ?? null
  const ownsCurrent = !current || current.owner === access.username || access.isAdmin || !access.loaded
  const canWriteCurrent = access.canWrite && ownsCurrent

  const composerHint = !access.canWrite
    ? t('chat.readOnly', { defaultValue: 'You can read conversations but not write (plugin write permission required).' })
    : authNone
      ? t('chat.notAuthenticatedShort', { defaultValue: 'Claude is not signed in.' })
      : !runnerUp
        ? t('chat.runnerUnavailable', { defaultValue: 'The assistant runner is not available right now.' })
        : !ownsCurrent
          ? t('chat.notOwner', { defaultValue: 'This conversation belongs to another user (read-only).' })
          : undefined

  const chatError = useMemo(() => {
    if (!chat.error) return null
    if (chat.error === STREAM_LOST) return t('chat.streamLost', { defaultValue: 'Lost the live connection to the runner; the transcript was reloaded.' })
    return chat.error
  }, [chat.error, t])

  const listProps = {
    sessions, selectedId, onSelect: (id: string) => selectSession(id), onNew: () => void createSession(),
    onClose: (s: SessionOut) => setClosing(s), canWrite: access.canWrite, canCreate: canChat && !creating,
    isAdmin: access.isAdmin, showAll, onToggleAll: () => setShowAll((v) => !v), showArchived,
    onToggleArchived: () => setShowArchived((v) => !v), loading: sessionsLoading, creating, username: access.username,
  }

  // ── banners (runner / auth) ────────────────────────────────────────────────
  const banners = (
    <>
      {status && !runnerUp && (
        <InfoBox tone="warning" icon="power_off"
          msg={<>
            <strong>{t('chat.runnerDownTitle', { defaultValue: 'Runner not available' })}</strong>{' — '}
            {t('chat.runnerDownBody', { defaultValue: 'status "{{status}}"', status: status.runner?.status ?? '?' })}
            {status.runner_error ? `: ${status.runner_error}` : status.runner?.error ? `: ${status.runner.error}` : ''}
            {' '}
            <LinkButton label={t('chat.openSettings', { defaultValue: 'Open settings' })} onClick={goSettings} />
          </>} />
      )}
      {status && authNone && (
        <InfoBox tone="warning" icon="key_off"
          msg={<>
            <strong>{t('chat.notAuthenticated', { defaultValue: 'Claude is not signed in.' })}</strong>{' '}
            {access.isAdmin
              ? <>{t('chat.notAuthenticatedAdmin', { defaultValue: 'Sign in with your Claude subscription or store an API key in the plugin settings.' })}{' '}<LinkButton label={t('chat.openSettings', { defaultValue: 'Open settings' })} onClick={goSettings} /></>
              : t('chat.notAuthenticatedUser', { defaultValue: 'Ask an administrator to connect the assistant.' })}
          </>} />
      )}
      {chat.runnerUnavailable && runnerUp && (
        <InfoBox tone="warning" icon="cloud_off"
          msg={<>{t('chat.runnerUnavailable', { defaultValue: 'The assistant runner is not available right now.' })}{' '}<LinkButton label={t('chat.openSettings', { defaultValue: 'Open settings' })} onClick={goSettings} /></>} />
      )}
    </>
  )

  // ── layout ─────────────────────────────────────────────────────────────────
  const main = (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0 }}>
      {/* conversation header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: compact ? '0.45rem 0.6rem' : '0.7rem 1.25rem', borderBottom: '1px solid var(--lx-border-soft)', flexShrink: 0, minHeight: 44 }}>
        {pickerMode ? (
          <SessionPicker {...listProps} />
        ) : (
          <>
            <Icon name="smart_toy" size={20} style={{ color: 'var(--lx-accent)' }} />
            <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
              <span style={{ fontSize: '0.9rem', fontWeight: 600, color: 'var(--lx-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {current ? current.title : t('chat.title', { defaultValue: 'AI assistant' })}
              </span>
              <span style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {current
                  ? [current.model ?? status?.model, current.permission_mode ?? status?.permission_mode, current.total_cost_usd > 0 ? fmtCost(current.total_cost_usd) : null].filter(Boolean).join(' · ') || t('chat.subtitle', { defaultValue: 'Claude Code in a sandboxed workspace' })
                  : t('chat.subtitle', { defaultValue: 'Claude Code in a sandboxed workspace' })}
              </span>
            </div>
          </>
        )}
        {current && (
          <Badge tone={sessionTone(chat.phase === 'closed' ? 'closed' : chat.phase).tone} pulse={sessionTone(chat.phase).pulse} label={statusLabel(chat.phase === 'closed' ? 'closed' : chat.phase)} />
        )}
        {current && <IconButton icon="refresh" size={16} title={t('common.refresh', { defaultValue: 'Refresh' })} onClick={() => void chat.reload()} disabled={chat.loading} />}
        {!compact && <IconButton icon="settings" size={18} title={t('common.settings', { defaultValue: 'Settings' })} onClick={goSettings} />}
      </div>

      {/* transcript */}
      {!selectedId ? (
        <div className="lxai-scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: compact ? '0.6rem 0.7rem' : '1rem 1.25rem' }}>
          <div style={{ maxWidth: 860, margin: '0 auto' }}>
            {banners}
            {listError && <ErrorBox msg={listError} onDismiss={() => setListError(null)} />}
            <EmptyState compact={compact} icon="forum"
              title={sessions.length ? t('chat.pickOne', { defaultValue: 'Pick a conversation or start a new one.' }) : t('chat.emptyTitle', { defaultValue: 'No conversation yet.' })}
              hint={t('chat.emptyHint', { defaultValue: 'Claude Code works inside the shared AI workspace repository and knows this Lyndrix installation through the generated instructions. Tool calls ask for your permission.' })}
              action={<Button variant="primary" icon="add" label={t('session.new', { defaultValue: 'New conversation' })} onClick={() => void createSession()} disabled={!canChat} busy={creating} />}
            />
          </div>
        </div>
      ) : chat.loading && chat.items.length === 0 ? (
        <div style={{ flex: 1, minHeight: 0, padding: compact ? '0 0.7rem' : '0 1.25rem' }}><Spinner label={t('common.loading', { defaultValue: 'Loading…' })} compact={compact} /></div>
      ) : (
        <Transcript
          items={chat.items} phase={chat.phase} onDecide={(id, d) => void chat.approve(id, d)} canWrite={canWriteCurrent} compact={compact}
          header={<>
            {banners}
            {chatError && <ErrorBox msg={chatError} onDismiss={chat.dismissError} />}
            {listError && <ErrorBox msg={listError} onDismiss={() => setListError(null)} />}
            {chat.items.length === 0 && !chat.loading && (
              <p style={{ margin: '0.5rem 0', fontSize: '0.78rem', color: 'var(--lx-text-muted)', textAlign: 'center' }}>
                {t('chat.freshSession', { defaultValue: 'New conversation — describe what you want Claude to look at or do.' })}
              </p>
            )}
          </>}
        />
      )}

      {/* composer */}
      {selectedId && (
        <Composer
          phase={chat.phase}
          disabled={!canWriteCurrent || !canChat}
          disabledHint={composerHint}
          onSend={(text) => void chat.send(text)}
          onInterrupt={() => void chat.interrupt()}
          compact={compact}
          autoFocus={!compact}
          permissionMode={current?.permission_mode ?? status?.permission_mode ?? null}
          isAdmin={access.isAdmin}
          onChangeMode={canWriteCurrent ? async (mode) => {
            try {
              await assistantApi.setPermissionMode(selectedId, mode)
              await loadSessions(true)
            } catch (err) {
              setListError((err as Error).message)
            }
          } : undefined}
        />
      )}
    </div>
  )

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: compact ? 0 : 420, width: '100%', overflow: 'hidden' }}>
      {!pickerMode && (
        <aside style={{ width: 272, flexShrink: 0, borderRight: '1px solid var(--lx-border-soft)', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          {sessionsLoading && sessions.length === 0 ? <Spinner label={t('common.loading', { defaultValue: 'Loading…' })} compact /> : <SessionList {...listProps} />}
        </aside>
      )}
      {main}

      {closing && (
        <Modal title={t('session.archiveTitle', { defaultValue: 'Close conversation?' })} onClose={() => setClosing(null)} width={460}
          footer={<>
            <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={() => setClosing(null)} />
            <Button variant="danger" icon="archive" label={t('session.archive', { defaultValue: 'Close & archive' })} onClick={() => void closeSession(closing)} />
          </>}>
          <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--lx-text)' }}>
            {t('session.archiveBody', { defaultValue: '"{{title}}" is closed in the runner and moved to the archive. The transcript stays readable.', title: closing.title })}
          </p>
        </Modal>
      )}
    </div>
  )
}
