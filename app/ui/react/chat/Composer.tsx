import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Icon } from '../shared'
import type { Phase } from './model'

const MAX_ROWS = 10

/** Approval modes the operator can switch per session. `auto` = run every tool the
 *  deny/ask guardrails do not stop; `bypassPermissions` also drops the guardrails (admins). */
const MODES: { id: string; icon: string; labelKey: string; label: string; hintKey: string; hint: string; admin?: boolean }[] = [
  { id: 'default', icon: 'help_outline', labelKey: 'composer.mode.default', label: 'Ask', hintKey: 'composer.modeHint.default', hint: 'Every tool call asks for your approval.' },
  { id: 'acceptEdits', icon: 'edit_note', labelKey: 'composer.mode.acceptEdits', label: 'Edits', hintKey: 'composer.modeHint.acceptEdits', hint: 'File edits in the workspace run without asking; everything else asks.' },
  { id: 'auto', icon: 'bolt', labelKey: 'composer.mode.auto', label: 'Auto', hintKey: 'composer.modeHint.auto', hint: 'Runs every tool call unless a deny rule blocks it or an ask rule flags it.' },
  { id: 'plan', icon: 'visibility', labelKey: 'composer.mode.plan', label: 'Plan', hintKey: 'composer.modeHint.plan', hint: 'Read-only: nothing is changed.' },
  { id: 'bypassPermissions', icon: 'warning_amber', labelKey: 'composer.mode.bypass', label: 'Bypass', hintKey: 'composer.modeHint.bypass', hint: 'Allows everything and disables the guardrails — administrators only.', admin: true },
]

export default function Composer({ phase, disabled, disabledHint, onSend, onInterrupt, compact, autoFocus, permissionMode, onChangeMode, isAdmin }: {
  phase: Phase
  /** No session / no permission / runner down — the textarea is locked. */
  disabled: boolean
  disabledHint?: string
  onSend: (text: string) => void
  onInterrupt: () => void
  compact: boolean
  autoFocus?: boolean
  /** Current approval mode of the session (shown + switchable when onChangeMode is given). */
  permissionMode?: string | null
  onChangeMode?: (mode: string) => Promise<void> | void
  isAdmin?: boolean
}) {
  const { t } = useTranslation('ai_assistant')
  const [text, setText] = useState('')
  const [interrupting, setInterrupting] = useState(false)
  const [switching, setSwitching] = useState<string | null>(null)
  const changeMode = async (mode: string) => {
    if (!onChangeMode || mode === permissionMode || switching) return
    setSwitching(mode)
    try { await onChangeMode(mode) } finally { setSwitching(null) }
  }
  const modes = MODES.filter((m) => !m.admin || isAdmin || m.id === permissionMode)
  const ref = useRef<HTMLTextAreaElement>(null)
  const running = phase === 'running' || phase === 'waiting_approval'
  const locked = disabled || running || phase === 'closed'

  // Grow with the content up to MAX_ROWS lines.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    const line = parseFloat(getComputedStyle(el).lineHeight) || 20
    const max = line * MAX_ROWS + 16
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
  }, [text])

  useEffect(() => {
    if (!running) setInterrupting(false)
  }, [running])

  useEffect(() => {
    if (autoFocus && !locked) ref.current?.focus()
  }, [autoFocus, locked])

  const submit = () => {
    const v = text.trim()
    if (!v || locked) return
    onSend(v)
    setText('')
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const placeholder = phase === 'closed'
    ? t('composer.closed', { defaultValue: 'This conversation is archived.' })
    : running
      ? t('composer.running', { defaultValue: 'Claude is working — wait or interrupt…' })
      : disabled && disabledHint
        ? disabledHint
        : t('composer.placeholder', { defaultValue: 'Ask Claude about this installation… (Enter sends, Shift+Enter for a new line)' })

  return (
    <div style={{ padding: compact ? '0.5rem 0.6rem 0.6rem' : '0.75rem 1.25rem 1rem', borderTop: '1px solid var(--lx-border-soft)', flexShrink: 0 }}>
      <div style={{ maxWidth: compact ? '100%' : 860, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <textarea
            ref={ref}
            className="lx-textarea lxai-textarea"
            rows={1}
            value={text}
            placeholder={placeholder}
            disabled={locked}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label={t('composer.ariaLabel', { defaultValue: 'Message to Claude' })}
            style={{ flex: 1, fontSize: compact ? '0.8rem' : '0.85rem' }}
          />
          {running ? (
            <button type="button" className="lx-btn lx-btn--danger lx-btn--sm" onClick={() => { setInterrupting(true); onInterrupt() }} disabled={interrupting}
              title={t('composer.interruptHint', { defaultValue: 'Stop the current turn' })} style={{ height: 36 }}>
              {interrupting ? <span className="lx-spinner" style={{ width: 12, height: 12, borderWidth: 2 }} /> : <Icon name="stop" size={16} />}
              {!compact && t('composer.interrupt', { defaultValue: 'Interrupt' })}
            </button>
          ) : (
            <button type="button" className="lx-btn lx-btn--primary lx-btn--sm" onClick={submit} disabled={locked || !text.trim()}
              title={t('composer.send', { defaultValue: 'Send' })} aria-label={t('composer.send', { defaultValue: 'Send' })} style={{ height: 36 }}>
              <Icon name="send" size={16} />
              {!compact && t('composer.send', { defaultValue: 'Send' })}
            </button>
          )}
        </div>
        <div style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {!compact && <span>{t('composer.hint', { defaultValue: 'Enter sends · Shift+Enter inserts a line break · tool calls may ask for your permission.' })}</span>}
          {onChangeMode && (
            <div role="radiogroup" aria-label={t('composer.modeLabel', { defaultValue: 'Approval mode' })}
              style={{ display: 'inline-flex', gap: 2, padding: 2, borderRadius: 'var(--lx-radius-sm)', border: '1px solid var(--lx-border-soft)', background: 'var(--lx-surface)', marginLeft: 'auto' }}>
              {modes.map((m) => {
                const active = m.id === permissionMode
                const danger = m.id === 'bypassPermissions'
                return (
                  <button key={m.id} type="button" role="radio" aria-checked={active} disabled={disabled || phase === 'closed' || !!switching}
                    onClick={() => void changeMode(m.id)}
                    title={t(m.hintKey, { defaultValue: m.hint })}
                    className="lxai-modebtn"
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: 4, padding: compact ? '2px 6px' : '3px 8px', borderRadius: 'calc(var(--lx-radius-sm) - 2px)', border: 'none', cursor: 'pointer', fontSize: '0.68rem',
                      background: active ? (danger ? 'var(--lx-state-down)' : m.id === 'auto' ? 'var(--lx-accent)' : 'var(--lx-elevated)') : 'transparent',
                      color: active ? (m.id === 'auto' || danger ? '#fff' : 'var(--lx-text)') : 'var(--lx-text-muted)', fontWeight: active ? 600 : 400,
                    }}>
                    {switching === m.id ? <span className="lx-spinner" style={{ width: 10, height: 10, borderWidth: 2 }} /> : <Icon name={m.icon} size={13} />}
                    {(!compact || active) && t(m.labelKey, { defaultValue: m.label })}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
