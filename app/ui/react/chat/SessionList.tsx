import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { SessionOut } from '../lib/api'
import { fmtCost, fmtRelative } from '../lib/format'
import { Button, Icon, IconButton, StatusDot, useTick } from '../shared'
import type { Tone } from '../shared'

export function sessionTone(status: string): { tone: Tone; pulse: boolean } {
  if (status === 'running') return { tone: 'accent', pulse: true }
  if (status === 'waiting_approval') return { tone: 'paused', pulse: true }
  if (status === 'closed') return { tone: 'muted', pulse: false }
  return { tone: 'up', pulse: false }
}

export function useStatusLabel() {
  const { t } = useTranslation('ai_assistant')
  return (status: string) =>
    status === 'running' ? t('session.status.running', { defaultValue: 'working' })
    : status === 'waiting_approval' ? t('session.status.waitingApproval', { defaultValue: 'awaiting approval' })
    : status === 'closed' ? t('session.status.closed', { defaultValue: 'archived' })
    : t('session.status.idle', { defaultValue: 'idle' })
}

interface Props {
  sessions: SessionOut[]
  selectedId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onClose: (s: SessionOut) => void
  canWrite: boolean
  canCreate: boolean
  isAdmin: boolean
  showAll: boolean
  onToggleAll: () => void
  showArchived: boolean
  onToggleArchived: () => void
  loading: boolean
  creating: boolean
  username: string | null
}

function Row({ s, active, onSelect, onClose, canWrite, showOwner, now }: {
  s: SessionOut; active: boolean; onSelect: () => void; onClose: () => void; canWrite: boolean; showOwner: boolean; now: number
}) {
  const { t, i18n } = useTranslation('ai_assistant')
  const statusLabel = useStatusLabel()
  const { tone, pulse } = sessionTone(s.status)
  const meta: string[] = []
  if (showOwner) meta.push(s.owner)
  meta.push(fmtRelative(s.last_activity, i18n.language, t('time.justNow', { defaultValue: 'just now' }), now))
  if (s.turns > 0) meta.push(t('session.turns', { defaultValue: '{{count}} turns', count: s.turns }))
  if (s.total_cost_usd > 0) meta.push(fmtCost(s.total_cost_usd))
  return (
    <div className={`lxai-row${active ? ' lxai-row--active' : ''}`} style={{ display: 'flex', alignItems: 'stretch', gap: 4 }}>
      <button type="button" className="lxai-card-btn" onClick={onSelect} aria-current={active}
        style={{ padding: '0.5rem 0.6rem', gap: 8, alignItems: 'flex-start', minWidth: 0 }}>
        <span style={{ marginTop: 5 }}><StatusDot tone={tone} pulse={pulse} title={statusLabel(s.status)} /></span>
        <span style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
          <span style={{ fontSize: '0.8rem', fontWeight: active ? 600 : 500, color: 'var(--lx-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.title}>
            {s.title || t('session.untitled', { defaultValue: 'New conversation' })}
          </span>
          <span style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{meta.join(' · ')}</span>
        </span>
      </button>
      {canWrite && s.status !== 'closed' && !s.archived && (
        <span className="lxai-row-actions" style={{ display: 'flex', alignItems: 'center', paddingRight: 2 }}>
          <IconButton icon="archive" size={16} title={t('session.archive', { defaultValue: 'Close & archive' })} onClick={onClose} />
        </span>
      )}
    </div>
  )
}

export default function SessionList(props: Props) {
  const { t } = useTranslation('ai_assistant')
  const now = useTick(30_000)
  const { sessions, selectedId, onSelect, onNew, onClose, canWrite, canCreate, isAdmin, showAll, onToggleAll, showArchived, onToggleArchived, loading, creating, username } = props
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ padding: '0.75rem 0.75rem 0.5rem', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <Button variant="primary" icon="add" label={t('session.new', { defaultValue: 'New conversation' })} onClick={onNew} disabled={!canCreate} busy={creating} />
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span className="lx-eyebrow">{t('session.listTitle', { defaultValue: 'Conversations' })}</span>
          <span style={{ display: 'flex', gap: 2 }}>
            {isAdmin && <IconButton icon="groups" size={16} active={showAll} title={t('session.showAll', { defaultValue: 'All users' })} onClick={onToggleAll} />}
            <IconButton icon="inventory_2" size={16} active={showArchived} title={t('session.showArchived', { defaultValue: 'Show archived' })} onClick={onToggleArchived} />
          </span>
        </div>
      </div>
      <div className="lxai-scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '0 0.4rem 0.5rem' }}>
        {sessions.length === 0 && !loading && (
          <p style={{ margin: '1rem 0.5rem', fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>
            {showArchived ? t('session.emptyArchived', { defaultValue: 'No archived conversations.' }) : t('session.empty', { defaultValue: 'No conversations yet.' })}
          </p>
        )}
        {sessions.map((s) => (
          <Row key={s.id} s={s} now={now} active={s.id === selectedId} onSelect={() => onSelect(s.id)} onClose={() => onClose(s)}
            canWrite={canWrite && (s.owner === username || isAdmin)} showOwner={showAll && s.owner !== username} />
        ))}
      </div>
    </div>
  )
}

/** Compact alternative for panel/narrow mode: current title + dropdown. */
export function SessionPicker(props: Props & { onOpenSettings?: () => void }) {
  const { t } = useTranslation('ai_assistant')
  const now = useTick(30_000)
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const current = props.sessions.find((s) => s.id === props.selectedId) ?? null
  const statusLabel = useStatusLabel()

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); window.removeEventListener('keydown', onKey) }
  }, [open])

  const { tone, pulse } = sessionTone(current?.status ?? 'idle')
  return (
    <div ref={wrap} style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 4, minWidth: 0, flex: 1 }}>
      <button type="button" className="lx-btn lx-btn--ghost lx-btn--sm" onClick={() => setOpen((o) => !o)} aria-haspopup="listbox" aria-expanded={open}
        style={{ minWidth: 0, maxWidth: '100%', justifyContent: 'flex-start', gap: 6 }}>
        {current && <StatusDot tone={tone} pulse={pulse} title={statusLabel(current.status)} />}
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--lx-text)' }}>
          {current ? current.title : t('session.pick', { defaultValue: 'Choose a conversation' })}
        </span>
        <Icon name={open ? 'expand_less' : 'expand_more'} size={16} />
      </button>
      <IconButton icon="add" size={18} title={t('session.new', { defaultValue: 'New conversation' })} onClick={props.onNew} disabled={!props.canCreate || props.creating} />
      {open && (
        <div className="lx-popover lxai-scroll" role="listbox" style={{ position: 'absolute', top: '100%', left: 0, marginTop: 4, width: 'min(340px, 92vw)', maxHeight: 320, overflowY: 'auto', zIndex: 20, padding: '0.3rem' }}>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 2, padding: '0 0.2rem 0.2rem' }}>
            {props.isAdmin && <IconButton icon="groups" size={15} active={props.showAll} title={t('session.showAll', { defaultValue: 'All users' })} onClick={props.onToggleAll} />}
            <IconButton icon="inventory_2" size={15} active={props.showArchived} title={t('session.showArchived', { defaultValue: 'Show archived' })} onClick={props.onToggleArchived} />
          </div>
          {props.sessions.length === 0 && (
            <p style={{ margin: '0.5rem', fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{t('session.empty', { defaultValue: 'No conversations yet.' })}</p>
          )}
          {props.sessions.map((s) => (
            <Row key={s.id} s={s} now={now} active={s.id === props.selectedId} onSelect={() => { props.onSelect(s.id); setOpen(false) }} onClose={() => { props.onClose(s); setOpen(false) }}
              canWrite={props.canWrite && (s.owner === props.username || props.isAdmin)} showOwner={props.showAll && s.owner !== props.username} />
          ))}
        </div>
      )}
    </div>
  )
}
