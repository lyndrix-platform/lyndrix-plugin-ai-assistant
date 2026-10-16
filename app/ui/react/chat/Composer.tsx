import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Icon } from '../shared'
import type { Phase } from './model'

const MAX_ROWS = 10

export default function Composer({ phase, disabled, disabledHint, onSend, onInterrupt, compact, autoFocus }: {
  phase: Phase
  /** No session / no permission / runner down — the textarea is locked. */
  disabled: boolean
  disabledHint?: string
  onSend: (text: string) => void
  onInterrupt: () => void
  compact: boolean
  autoFocus?: boolean
}) {
  const { t } = useTranslation('ai_assistant')
  const [text, setText] = useState('')
  const [interrupting, setInterrupting] = useState(false)
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
        {!compact && (
          <div style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', display: 'flex', justifyContent: 'space-between', gap: 8 }}>
            <span>{t('composer.hint', { defaultValue: 'Enter sends · Shift+Enter inserts a line break · tool calls may ask for your permission.' })}</span>
          </div>
        )}
      </div>
    </div>
  )
}
