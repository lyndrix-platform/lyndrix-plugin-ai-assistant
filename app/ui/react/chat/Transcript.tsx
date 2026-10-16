import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { ApprovalDecision } from '../lib/api'
import { fmtCost, fmtDuration, prettyJson, summarizeToolInput, toolIcon, truncate } from '../lib/format'
import { Button, Icon } from '../shared'
import RichText from './RichText'
import type { Phase, TranscriptItem } from './model'

const NEAR_BOTTOM_PX = 90
const RESULT_PREVIEW = 160
const OUTPUT_CAP = 6000

// ─── Blocks ─────────────────────────────────────────────────────────────────

function UserBubble({ text, pending, compact }: { text: string; pending?: boolean; compact: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
      <div
        style={{
          maxWidth: compact ? '92%' : '78%',
          padding: compact ? '0.45rem 0.7rem' : '0.6rem 0.9rem',
          borderRadius: 'var(--lx-radius-md)',
          borderTopRightRadius: 4,
          background: 'linear-gradient(135deg, color-mix(in srgb, var(--lx-accent) 22%, var(--lx-surface)), color-mix(in srgb, var(--lx-accent-2) 18%, var(--lx-surface)))',
          border: '1px solid color-mix(in srgb, var(--lx-accent) 30%, transparent)',
          color: 'var(--lx-text)',
          fontSize: compact ? '0.8rem' : '0.86rem',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          opacity: pending ? 0.6 : 1,
        }}
      >
        {text}
      </div>
    </div>
  )
}

function AssistantBlock({ text, streaming, compact }: { text: string; streaming: boolean; compact: boolean }) {
  return (
    <div style={{ display: 'flex', gap: compact ? 8 : 12, alignItems: 'flex-start' }}>
      {!compact && (
        <span className="lx-card" style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 28, flexShrink: 0, color: 'var(--lx-accent)' }}>
          <Icon name="smart_toy" size={16} />
        </span>
      )}
      <div style={{ minWidth: 0, flex: 1, fontSize: compact ? '0.8rem' : '0.86rem', color: 'var(--lx-text)', lineHeight: 1.55 }}>
        <RichText text={text} className={streaming ? 'lxai-caret' : undefined} />
      </div>
    </div>
  )
}

function ThinkingBlock({ text }: { text: string }) {
  const { t } = useTranslation('ai_assistant')
  const [open, setOpen] = useState(false)
  return (
    <div style={{ fontSize: '0.74rem', color: 'var(--lx-text-muted)', fontStyle: 'italic' }}>
      <button type="button" className="lxai-link" onClick={() => setOpen((o) => !o)} style={{ color: 'var(--lx-text-muted)', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        <Icon name={open ? 'expand_less' : 'psychology'} size={14} />
        {t('chat.thinking', { defaultValue: 'Thinking…' })}
      </button>
      {open && <div style={{ marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{text}</div>}
      {!open && text && <span style={{ marginLeft: 6 }}>{truncate(text.replace(/\s+/g, ' '), 90)}</span>}
    </div>
  )
}

function ToolCard({ item, compact }: { item: Extract<TranscriptItem, { kind: 'tool' }>; compact: boolean }) {
  const { t } = useTranslation('ai_assistant')
  const [open, setOpen] = useState(false)
  const summary = summarizeToolInput(item.name, item.input)
  const res = item.result
  const tone = res ? (res.isError ? 'var(--lx-state-down)' : 'var(--lx-state-up)') : 'var(--lx-text-muted)'
  const preview = res ? truncate(res.output.trim().split(/\r?\n/).find((l) => l.trim()) ?? '', RESULT_PREVIEW) : ''
  const output = res ? (res.output.length > OUTPUT_CAP ? `${res.output.slice(0, OUTPUT_CAP)}\n… (${res.output.length - OUTPUT_CAP} more chars)` : res.output) : ''
  return (
    <div className="lx-card" style={{ padding: 0, overflow: 'hidden', borderLeft: `3px solid ${tone}` }}>
      <button type="button" className="lxai-card-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        style={{ padding: compact ? '0.4rem 0.6rem' : '0.5rem 0.75rem', gap: 8, alignItems: 'center' }}>
        <Icon name={toolIcon(item.name)} size={16} style={{ color: 'var(--lx-accent)', flexShrink: 0 }} />
        <span className="lx-mono" style={{ fontSize: '0.74rem', fontWeight: 600, color: 'var(--lx-text)', flexShrink: 0 }}>{item.name}</span>
        {summary && (
          <span className="lx-mono" style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: 1 }} title={summary}>
            {summary}
          </span>
        )}
        {!summary && <span style={{ flex: 1 }} />}
        {!res && <span className="lx-spinner" style={{ width: 12, height: 12, borderWidth: 2, flexShrink: 0 }} aria-label={t('chat.toolRunning', { defaultValue: 'running' })} />}
        {res && <Icon name={res.isError ? 'error' : 'check_circle'} size={15} style={{ color: tone, flexShrink: 0 }} />}
        <Icon name={open ? 'expand_less' : 'expand_more'} size={16} style={{ color: 'var(--lx-text-muted)', flexShrink: 0 }} />
      </button>
      {!open && preview && (
        <div className="lx-mono" style={{ padding: '0 0.75rem 0.45rem 2.25rem', fontSize: '0.7rem', color: res?.isError ? 'var(--lx-state-down)' : 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {preview}
        </div>
      )}
      {open && (
        <div style={{ padding: '0 0.75rem 0.65rem', display: 'flex', flexDirection: 'column', gap: 6 }}>
          {item.input !== undefined && (
            <>
              <span className="lx-eyebrow">{t('chat.toolInput', { defaultValue: 'Input' })}</span>
              <pre className="lx-log-panel lxai-pre" style={{ maxHeight: 260, padding: '0.5rem 0.65rem' }}>{prettyJson(item.input)}</pre>
            </>
          )}
          {res && (
            <>
              <span className="lx-eyebrow" style={{ color: res.isError ? 'var(--lx-state-down)' : undefined }}>
                {res.isError ? t('chat.toolError', { defaultValue: 'Error' }) : t('chat.toolResult', { defaultValue: 'Result' })}
              </span>
              <pre className="lx-terminal lxai-pre" style={{ maxHeight: 320, padding: '0.5rem 0.65rem' }}>{output || '∅'}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function ApprovalCard({ item, onDecide, canDecide, compact }: {
  item: Extract<TranscriptItem, { kind: 'approval' }>
  onDecide: (approvalId: string, decision: ApprovalDecision) => void
  canDecide: boolean
  compact: boolean
}) {
  const { t } = useTranslation('ai_assistant')
  const [busy, setBusy] = useState<ApprovalDecision | null>(null)
  const [showInput, setShowInput] = useState(false)
  const decided = item.decision !== null
  const summary = summarizeToolInput(item.name, item.input)
  const decisionLabel = (d: string) =>
    d === 'allow' ? t('approval.allowed', { defaultValue: 'allowed' })
    : d === 'allow_always' ? t('approval.alwaysAllowed', { defaultValue: 'always allowed' })
    : d === 'deny' ? t('approval.denied', { defaultValue: 'denied' })
    : d === 'expired' ? t('approval.expired', { defaultValue: 'expired' })
    : d
  const decide = (d: ApprovalDecision) => {
    setBusy(d)
    onDecide(item.approvalId, d)
  }
  const tone = decided ? (item.decision === 'deny' ? 'var(--lx-state-down)' : 'var(--lx-state-up)') : 'var(--lx-state-paused)'
  return (
    <div className="lx-card" style={{ padding: compact ? '0.55rem 0.7rem' : '0.7rem 0.9rem', borderLeft: `3px solid ${tone}`, display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Icon name="shield" size={16} style={{ color: tone }} />
        <span style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--lx-text)' }}>
          {t('approval.title', { defaultValue: 'Permission requested' })}
        </span>
        <span className="lx-badge lx-badge--muted lx-mono">{item.name}</span>
        {decided && (
          <span className={`lx-badge ${item.decision === 'deny' ? 'lx-badge--down' : item.decision === 'expired' ? 'lx-badge--muted' : 'lx-badge--up'}`} style={{ marginLeft: 'auto' }}>
            <span className="lx-dot" />{decisionLabel(item.decision!)}
          </span>
        )}
      </div>
      {summary && <div className="lx-mono" style={{ fontSize: '0.74rem', color: 'var(--lx-text)', wordBreak: 'break-word' }}>{summary}</div>}
      {item.input !== undefined && (
        <div>
          <button type="button" className="lxai-link" onClick={() => setShowInput((s) => !s)} style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', textDecoration: 'none' }}>
            {showInput ? t('approval.hideInput', { defaultValue: 'Hide details' }) : t('approval.showInput', { defaultValue: 'Show details' })}
          </button>
          {showInput && <pre className="lx-log-panel lxai-pre" style={{ marginTop: 6, maxHeight: 240, padding: '0.5rem 0.65rem' }}>{prettyJson(item.input)}</pre>}
        </div>
      )}
      {!decided && (
        <div style={{ display: 'flex', gap: '0.45rem', flexWrap: 'wrap' }}>
          <Button variant="primary" icon="check" label={t('approval.allow', { defaultValue: 'Allow' })} onClick={() => decide('allow')} disabled={!canDecide || busy !== null} busy={busy === 'allow'} />
          <Button variant="danger" icon="block" label={t('approval.deny', { defaultValue: 'Deny' })} onClick={() => decide('deny')} disabled={!canDecide || busy !== null} busy={busy === 'deny'} />
          <Button icon="done_all" label={t('approval.allowAlways', { defaultValue: 'Always allow' })} onClick={() => decide('allow_always')} disabled={!canDecide || busy !== null} busy={busy === 'allow_always'}
            title={t('approval.allowAlwaysHint', { defaultValue: 'Allow this tool for the rest of the session without asking again.' })} />
        </div>
      )}
    </div>
  )
}

function ResultFooter({ item }: { item: Extract<TranscriptItem, { kind: 'result' }> }) {
  const { t } = useTranslation('ai_assistant')
  const parts: string[] = []
  if (item.numTurns !== null) parts.push(t('result.turns', { defaultValue: '{{count}} turns', count: item.numTurns }))
  if (item.durationMs !== null) parts.push(fmtDuration(item.durationMs))
  if (item.costUsd !== null) parts.push(fmtCost(item.costUsd))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '0 0 0.25rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.68rem', color: item.isError ? 'var(--lx-state-down)' : 'var(--lx-text-muted)' }}>
        <Icon name={item.isError ? 'error_outline' : 'check'} size={13} />
        <span>{item.isError ? t('result.failed', { defaultValue: 'Turn failed' }) : t('result.done', { defaultValue: 'Done' })}</span>
        {parts.length > 0 && <span>· {parts.join(' · ')}</span>}
      </div>
      {item.isError && item.text && (
        <pre className="lx-log-panel lxai-pre" style={{ padding: '0.45rem 0.6rem', color: 'var(--lx-state-down)', maxHeight: 200 }}>{item.text}</pre>
      )}
    </div>
  )
}

function SystemLine({ text, tone }: { text: string; tone: 'muted' | 'error' }) {
  const { t } = useTranslation('ai_assistant')
  let label = text
  if (text.startsWith('auto:')) {
    const [, decision, ...rest] = text.split(':')
    label = t('approval.autoLine', { defaultValue: 'Tool {{name}}: {{decision}} automatically by policy', name: rest.join(':'), decision: decision === 'deny' ? t('approval.denied', { defaultValue: 'denied' }) : t('approval.allowed', { defaultValue: 'allowed' }) })
  } else if (text.startsWith('approval:')) {
    const d = text.slice('approval:'.length)
    const decision =
      d === 'allow' ? t('approval.allowed', { defaultValue: 'allowed' })
      : d === 'allow_always' ? t('approval.alwaysAllowed', { defaultValue: 'always allowed' })
      : d === 'deny' ? t('approval.denied', { defaultValue: 'denied' })
      : d
    label = t('approval.resolvedLine', { defaultValue: 'Tool permission: {{decision}}', decision })
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.72rem', color: tone === 'error' ? 'var(--lx-state-down)' : 'var(--lx-text-muted)', wordBreak: 'break-word' }}>
      <Icon name={tone === 'error' ? 'error_outline' : 'shield'} size={13} />
      <span>{label}</span>
    </div>
  )
}

// ─── Transcript ─────────────────────────────────────────────────────────────

export default function Transcript({ items, phase, onDecide, canWrite, compact, header }: {
  items: TranscriptItem[]
  phase: Phase
  onDecide: (approvalId: string, decision: ApprovalDecision) => void
  canWrite: boolean
  compact: boolean
  header?: React.ReactNode
}) {
  const { t } = useTranslation('ai_assistant')
  const ref = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  // Follow the stream while the user is near the bottom; leave them alone otherwise.
  const onScroll = () => {
    const el = ref.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX
  }
  useEffect(() => {
    const el = ref.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [items, phase])

  const working = phase === 'running' && !items.some((it) => it.kind === 'assistant' && it.streaming)
  const lastItem = items[items.length - 1]
  const showWorking = working && (!lastItem || lastItem.kind === 'user' || lastItem.kind === 'tool' || lastItem.kind === 'approval' || lastItem.kind === 'thinking' || lastItem.kind === 'system')

  return (
    <div ref={ref} onScroll={onScroll} className="lxai-scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: compact ? '0.6rem 0.7rem' : '1rem 1.25rem' }}>
      <div style={{ maxWidth: compact ? '100%' : 860, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: compact ? '0.6rem' : '0.85rem' }}>
        {header}
        {items.map((it) => {
          switch (it.kind) {
            case 'user': return <UserBubble key={it.id} text={it.text} pending={it.pending} compact={compact} />
            case 'assistant': return <AssistantBlock key={it.id} text={it.text} streaming={it.streaming} compact={compact} />
            case 'thinking': return <ThinkingBlock key={it.id} text={it.text} />
            case 'tool': return <ToolCard key={it.id} item={it} compact={compact} />
            case 'approval': return <ApprovalCard key={it.id} item={it} onDecide={onDecide} canDecide={canWrite} compact={compact} />
            case 'result': return <ResultFooter key={it.id} item={it} />
            case 'system': return <SystemLine key={it.id} text={it.text} tone={it.tone} />
            default: return null
          }
        })}
        {showWorking && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.74rem', color: 'var(--lx-text-muted)' }}>
            <span className="lx-spinner" style={{ width: 14, height: 14, borderWidth: 2 }} />
            {phase === 'running' ? t('chat.working', { defaultValue: 'Claude is working…' }) : null}
          </div>
        )}
        {phase === 'waiting_approval' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.74rem', color: 'var(--lx-state-paused)' }}>
            <Icon name="pause_circle" size={15} />
            {t('chat.waitingApproval', { defaultValue: 'Waiting for your decision above.' })}
          </div>
        )}
      </div>
    </div>
  )
}
