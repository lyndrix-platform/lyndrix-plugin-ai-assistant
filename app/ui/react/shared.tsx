import React, { useEffect, useState } from 'react'
import { assistantApi, PERM_ADMIN, PERM_READ, PERM_WRITE, SHELL_PLUGIN_ID, spaNavigate } from './lib/api'
import type { AccessView } from './lib/api'

// Small presentational pieces + navigation helpers shared by chat and settings.
// Everything renders into the host DOM, so the host's .lx-* classes and
// --lx-* tokens apply. The few rules inline styles cannot express (hover,
// keyframes, scrollbars) live in <AiStyles/> under the lxai- prefix.

// ─── Location / navigation ──────────────────────────────────────────────────

/** Current pathname + search; re-renders on history navigation (spaNavigate
 *  dispatches popstate, so in-plugin links update the view without a reload). */
export function useLocation(): { pathname: string; search: string } {
  const read = () => ({ pathname: window.location.pathname, search: window.location.search })
  const [loc, setLoc] = useState(read)
  useEffect(() => {
    const onChange = () => setLoc(read())
    window.addEventListener('popstate', onChange)
    return () => window.removeEventListener('popstate', onChange)
  }, [])
  return loc
}

/** Base of this plugin's routes in the shell: /apps/<id>/ai */
export function pluginBase(): string {
  const p = window.location.pathname.replace(/\/+$/, '')
  const i = p.indexOf('/ai/')
  return i >= 0 ? p.slice(0, i + '/ai'.length) : `/apps/${SHELL_PLUGIN_ID}/ai`
}

export const chatPath = () => `${pluginBase()}/chat`
export const settingsPath = () => `${pluginBase()}/settings`
export const goChat = () => spaNavigate(chatPath())
export const goSettings = () => spaNavigate(settingsPath())

// ─── Permissions ────────────────────────────────────────────────────────────

export interface Access {
  loaded: boolean
  username: string | null
  isAdmin: boolean
  canWrite: boolean
  canRead: boolean
  can: (permission: string) => boolean
}

/** Mirror of the shell's `can()` (lyndrix-ui/src/lib/access.ts) over the
 *  server-derived access view. Display hygiene only — the API enforces
 *  regardless. Unknown → read/write optimistic, admin pessimistic. */
function evaluate(a: AccessView | null, permission: string): boolean {
  if (!a) return !permission.endsWith(':api:admin')
  if (a.is_superadmin) return true
  if (a.permissions.includes(permission)) return true
  if (permission.startsWith('plugin:')) {
    if (permission.endsWith(':api:read') && a.permissions.includes('api:read')) return true
    if (permission.endsWith(':api:write') && a.permissions.includes('api:write')) return true
  }
  return false
}

export function useAccess(): Access {
  const [view, setView] = useState<AccessView | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    let alive = true
    assistantApi.access()
      .then((a) => { if (alive) { setView(a); setLoaded(true) } })
      .catch(() => { if (alive) setLoaded(true) })
    return () => { alive = false }
  }, [])
  return {
    loaded,
    username: view?.username ?? null,
    isAdmin: evaluate(view, PERM_ADMIN),
    canWrite: evaluate(view, PERM_WRITE),
    canRead: evaluate(view, PERM_READ),
    can: (p) => evaluate(view, p),
  }
}

// ─── Hooks ──────────────────────────────────────────────────────────────────

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => (typeof window !== 'undefined' ? window.matchMedia(query).matches : false))
  useEffect(() => {
    const mq = window.matchMedia(query)
    const onChange = () => setMatches(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [query])
  return matches
}

/** A success notice that clears itself. */
export function useNotice(ttlMs = 4000): [string | null, (msg: string | null) => void] {
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => {
    if (!notice) return
    const t = window.setTimeout(() => setNotice(null), ttlMs)
    return () => window.clearTimeout(t)
  }, [notice, ttlMs])
  return [notice, setNotice]
}

/** Re-renders every `ms` — for relative timestamps. */
export function useTick(ms: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(t)
  }, [ms])
  return now
}

// ─── Styles the inline approach cannot express ──────────────────────────────

const STYLE_ID = 'lxai-styles'
const CSS = `
@keyframes lxai-pulse { 0%,100% { opacity: 1; transform: scale(1);} 50% { opacity: .45; transform: scale(.8);} }
@keyframes lxai-blink { 0%,100% { opacity: 1;} 50% { opacity: 0;} }
.lxai-pulse { animation: lxai-pulse 1.2s ease-in-out infinite; }
.lxai-caret::after { content: '▍'; margin-left: 1px; color: var(--lx-accent); animation: lxai-blink 1s steps(1) infinite; }
.lxai-row { border-radius: var(--lx-radius-sm); transition: background .15s ease; }
.lxai-row:hover { background: var(--lx-elevated); }
.lxai-row--active { background: color-mix(in srgb, var(--lx-accent) 12%, transparent); }
.lxai-row--active:hover { background: color-mix(in srgb, var(--lx-accent) 16%, transparent); }
.lxai-row .lxai-row-actions { opacity: 0; transition: opacity .15s ease; }
.lxai-row:hover .lxai-row-actions, .lxai-row:focus-within .lxai-row-actions { opacity: 1; }
.lxai-scroll { scrollbar-width: thin; scrollbar-color: var(--lx-border-soft) transparent; }
.lxai-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
.lxai-scroll::-webkit-scrollbar-thumb { background: var(--lx-border-soft); border-radius: 8px; }
.lxai-pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: var(--lx-font-mono); font-size: 0.74rem; line-height: 1.5; }
.lxai-rich p { margin: 0 0 .55em; } .lxai-rich p:last-child { margin-bottom: 0; }
.lxai-rich code { font-family: var(--lx-font-mono); font-size: .8em; padding: .1em .35em; border-radius: 4px; background: color-mix(in srgb, var(--lx-text) 9%, transparent); }
.lxai-rich pre { margin: .35em 0 .6em; }
.lxai-rich ul { margin: 0 0 .55em; padding-left: 1.25em; } .lxai-rich li { margin: .15em 0; }
.lxai-rich h4 { margin: .6em 0 .3em; font-size: .9em; font-weight: 700; }
.lxai-rich a { color: var(--lx-accent); }
.lxai-card-btn { all: unset; display: flex; width: 100%; box-sizing: border-box; cursor: pointer; }
.lxai-card-btn:focus-visible { outline: 2px solid var(--lx-accent); outline-offset: 2px; border-radius: var(--lx-radius-sm); }
.lxai-textarea { resize: none; line-height: 1.45; max-height: 40vh; }
.lxai-link { all: unset; cursor: pointer; color: var(--lx-accent); text-decoration: underline; text-underline-offset: 2px; font: inherit; }
`

/** Injects the plugin's few CSS rules once per document. */
export function AiStyles() {
  useEffect(() => {
    if (document.getElementById(STYLE_ID)) return
    const el = document.createElement('style')
    el.id = STYLE_ID
    el.textContent = CSS
    document.head.appendChild(el)
  }, [])
  return null
}

// ─── Presentational ─────────────────────────────────────────────────────────

export function Icon({ name, size = 18, style, className }: { name: string; size?: number; style?: React.CSSProperties; className?: string }) {
  return <span className={`material-icons${className ? ` ${className}` : ''}`} aria-hidden style={{ fontSize: size, lineHeight: 1, ...style }}>{name}</span>
}

export function Button({
  label, onClick, disabled, variant = 'default', icon, title, type = 'button', busy,
}: {
  label: string
  onClick?: () => void
  disabled?: boolean
  variant?: 'default' | 'danger' | 'primary' | 'ghost'
  icon?: string
  title?: string
  type?: 'button' | 'submit'
  busy?: boolean
}) {
  const cls =
    variant === 'primary' ? 'lx-btn lx-btn--primary lx-btn--sm'
    : variant === 'danger' ? 'lx-btn lx-btn--danger lx-btn--sm'
    : variant === 'ghost' ? 'lx-btn lx-btn--ghost lx-btn--sm'
    : 'lx-btn lx-btn--secondary lx-btn--sm'
  return (
    <button type={type} onClick={onClick} disabled={disabled || busy} className={cls} title={title}>
      {busy ? <span className="lx-spinner" style={{ width: 12, height: 12, borderWidth: 2 }} /> : icon && <Icon name={icon} size={16} />}
      {label}
    </button>
  )
}

export function IconButton({ icon, onClick, title, disabled, danger, size = 18, active }: {
  icon: string
  onClick: () => void
  title: string
  disabled?: boolean
  danger?: boolean
  size?: number
  active?: boolean
}) {
  return (
    <button
      type="button"
      className={danger ? 'lx-icon-btn lx-icon-btn--danger' : 'lx-icon-btn'}
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-pressed={active}
      disabled={disabled}
      style={active ? { color: 'var(--lx-accent)', background: 'color-mix(in srgb, var(--lx-accent) 12%, transparent)' } : undefined}
    >
      <Icon name={icon} size={size} />
    </button>
  )
}

export function ErrorBox({ msg, onDismiss, action }: { msg: string; onDismiss?: () => void; action?: React.ReactNode }) {
  return (
    <div className="lx-card lx-card-accent lx-card-accent--danger" role="alert"
      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0.6rem 0.9rem', marginBottom: '0.75rem', color: 'var(--lx-state-down)', fontSize: '0.8rem' }}>
      <Icon name="error_outline" />
      <span style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>{msg}</span>
      {action}
      {onDismiss && <button type="button" className="lx-icon-btn" onClick={onDismiss} aria-label="dismiss" style={{ padding: 2 }}><Icon name="close" size={16} /></button>}
    </div>
  )
}

export function NoticeBox({ msg }: { msg: string }) {
  return (
    <div className="lx-card lx-card-accent lx-card-accent--success" role="status"
      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0.6rem 0.9rem', marginBottom: '0.75rem', color: 'var(--lx-state-up)', fontSize: '0.8rem' }}>
      <Icon name="check_circle" />
      <span>{msg}</span>
    </div>
  )
}

export function InfoBox({ msg, icon = 'info', action, tone = 'info' }: { msg: React.ReactNode; icon?: string; action?: React.ReactNode; tone?: 'info' | 'warning' }) {
  return (
    <div className={`lx-card lx-card-accent lx-card-accent--${tone}`} role="status"
      style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0.6rem 0.9rem', marginBottom: '0.75rem', color: 'var(--lx-text)', fontSize: '0.8rem' }}>
      <Icon name={icon} style={{ color: tone === 'warning' ? 'var(--lx-state-paused)' : 'var(--lx-accent)' }} />
      <span style={{ flex: 1, minWidth: 0 }}>{msg}</span>
      {action}
    </div>
  )
}

export function Spinner({ label, compact }: { label: string; compact?: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: compact ? '0.75rem 0' : '2rem 0', color: 'var(--lx-text-muted)', fontSize: '0.85rem' }}>
      <div className="lx-spinner" style={{ width: 18, height: 18 }} />
      {label}
    </div>
  )
}

export function EmptyState({ icon, title, hint, action, compact }: { icon: string; title: string; hint?: React.ReactNode; action?: React.ReactNode; compact?: boolean }) {
  return (
    <div className="lx-empty" style={compact ? { padding: '1.5rem 1rem', gap: 8 } : undefined}>
      <Icon name={icon} size={compact ? 28 : 34} />
      <p style={{ margin: 0, fontWeight: 600, color: 'var(--lx-text)', fontSize: compact ? '0.85rem' : undefined }}>{title}</p>
      {hint && <p style={{ margin: 0, fontSize: '0.8rem', maxWidth: 460 }}>{hint}</p>}
      {action}
    </div>
  )
}

export function Modal({ title, onClose, children, width = 640, footer }: { title: string; onClose: () => void; children: React.ReactNode; width?: number; footer?: React.ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="lx-modal-overlay" onClick={onClose} style={{ position: 'fixed', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 80 }}>
      <div className="lx-modal lx-card" role="dialog" aria-modal aria-label={title} onClick={(e) => e.stopPropagation()}
        style={{ width: `min(${width}px, 94vw)`, maxHeight: '90vh', display: 'flex', flexDirection: 'column', padding: '1.25rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.9rem' }}>
          <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 700, color: 'var(--lx-text)' }}>{title}</h3>
          <button type="button" className="lx-icon-btn" onClick={onClose} aria-label="close"><Icon name="close" /></button>
        </div>
        <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }} className="lxai-scroll">{children}</div>
        {footer && <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginTop: '0.9rem' }}>{footer}</div>}
      </div>
    </div>
  )
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap', marginBottom: '1.25rem' }}>
      <div>
        <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 700, color: 'var(--lx-text)' }}>{title}</h1>
        {subtitle && <p style={{ margin: '0.25rem 0 0', fontSize: '0.85rem', color: 'var(--lx-text-muted)' }}>{subtitle}</p>}
      </div>
      {actions && <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>{actions}</div>}
    </div>
  )
}

export type Tone = 'up' | 'down' | 'paused' | 'muted' | 'accent' | 'unknown'

export function Badge({ tone, label, dot = true, pulse }: { tone: Tone; label: string; dot?: boolean; pulse?: boolean }) {
  return (
    <span className={`lx-badge lx-badge--${tone}`}>
      {dot && <span className={`lx-dot${pulse ? ' lxai-pulse' : ''}`} />}
      {label}
    </span>
  )
}

const TONE_VAR: Record<Tone, string> = {
  up: 'var(--lx-state-up)',
  down: 'var(--lx-state-down)',
  paused: 'var(--lx-state-paused)',
  unknown: 'var(--lx-state-unknown, var(--lx-text-muted))',
  muted: 'var(--lx-text-muted)',
  accent: 'var(--lx-accent)',
}

export function StatusDot({ tone, pulse, size = 8, title }: { tone: Tone; pulse?: boolean; size?: number; title?: string }) {
  return (
    <span title={title} aria-hidden={!title} className={pulse ? 'lxai-pulse' : undefined}
      style={{ display: 'inline-block', width: size, height: size, borderRadius: '50%', background: TONE_VAR[tone], flexShrink: 0 }} />
  )
}

/** Label/value row for the status cards. */
export function KV({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', fontSize: '0.78rem', padding: '0.3rem 0', borderBottom: '1px solid var(--lx-border-soft)' }}>
      <span style={{ color: 'var(--lx-text-muted)', flexShrink: 0 }}>{label}</span>
      <span className={mono ? 'lx-mono' : undefined} style={{ color: 'var(--lx-text)', textAlign: 'right', minWidth: 0, overflowWrap: 'anywhere' }}>{children}</span>
    </div>
  )
}

export function Card({ title, icon, children, actions, style }: { title?: string; icon?: string; children: React.ReactNode; actions?: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <section className="lx-card" style={{ padding: '1rem 1.1rem', display: 'flex', flexDirection: 'column', gap: '0.6rem', ...style }}>
      {(title || actions) && (
        <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem' }}>
          <h2 className="lx-section-title" style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
            {icon && <Icon name={icon} size={18} style={{ color: 'var(--lx-accent)' }} />}
            {title}
          </h2>
          {actions && <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', flexWrap: 'wrap' }}>{actions}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

/** Inline text button (link-styled). */
export function LinkButton({ label, onClick }: { label: string; onClick: () => void }) {
  return <button type="button" className="lxai-link" onClick={onClick}>{label}</button>
}
