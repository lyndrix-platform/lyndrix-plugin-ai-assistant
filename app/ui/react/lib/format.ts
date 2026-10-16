/** Pure display helpers — no i18n strings here except via the passed language. */

/** The DB stores naive UTC (`isoformat()` without offset); treat such values as UTC. */
export function parseServerDate(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value)
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(value)
  const d = new Date(hasZone ? value : `${value}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

/** "3 minutes ago" in the UI language; `justNow` is the translated label for < 45 s. */
export function fmtRelative(value: string | number | null | undefined, lang: string, justNow: string, now = Date.now()): string {
  const d = parseServerDate(value)
  if (!d) return '—'
  const diffS = Math.round((d.getTime() - now) / 1000)
  const abs = Math.abs(diffS)
  if (abs < 45) return justNow
  let rtf: Intl.RelativeTimeFormat
  try {
    rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' })
  } catch {
    rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  }
  if (abs < 3600) return rtf.format(Math.round(diffS / 60), 'minute')
  if (abs < 86_400) return rtf.format(Math.round(diffS / 3600), 'hour')
  if (abs < 86_400 * 14) return rtf.format(Math.round(diffS / 86_400), 'day')
  return d.toLocaleDateString(lang)
}

export function fmtDateTime(value: string | number | null | undefined, lang: string): string {
  const d = parseServerDate(value)
  return d ? d.toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined || Number.isNaN(usd)) return '—'
  if (usd === 0) return '$0.00'
  return usd < 1 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s - m * 60)
  return `${m}m ${String(rest).padStart(2, '0')}s`
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return `${text.slice(0, Math.max(0, max - 1))}…`
}

function firstLine(text: string): string {
  const line = text.split(/\r?\n/).find((l) => l.trim().length > 0)
  return (line ?? '').trim()
}

/** Compact one-line description of a tool call, keyed on the well-known
 *  Claude Code tool inputs; falls back to the first string value / JSON. */
export function summarizeToolInput(name: string, input: unknown): string {
  if (input === null || input === undefined) return ''
  if (typeof input === 'string') return truncate(firstLine(input), 120)
  if (typeof input !== 'object') return truncate(String(input), 120)
  const obj = input as Record<string, unknown>
  const str = (k: string) => (typeof obj[k] === 'string' ? (obj[k] as string) : null)
  const n = name.toLowerCase()
  const pick =
    (n === 'bash' && (str('command') ?? str('description')))
    || ((n === 'read' || n === 'write' || n === 'edit' || n === 'multiedit' || n === 'notebookedit') && str('file_path'))
    || ((n === 'grep' || n === 'glob') && [str('pattern'), str('path')].filter(Boolean).join('  in  '))
    || (n === 'webfetch' && str('url'))
    || (n === 'websearch' && str('query'))
    || ((n === 'task' || n === 'agent') && (str('description') ?? str('prompt')))
    || (n === 'todowrite' && 'todos')
    || null
  if (pick) return truncate(firstLine(pick), 120)
  for (const v of Object.values(obj)) {
    if (typeof v === 'string' && v.trim()) return truncate(firstLine(v), 120)
  }
  try {
    return truncate(JSON.stringify(obj), 120)
  } catch {
    return ''
  }
}

export function prettyJson(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/** Material icon for a Claude Code tool name. */
export function toolIcon(name: string): string {
  const n = name.toLowerCase()
  if (n === 'bash') return 'terminal'
  if (n === 'read') return 'description'
  if (n === 'write' || n === 'edit' || n === 'multiedit' || n === 'notebookedit') return 'edit_note'
  if (n === 'grep' || n === 'glob') return 'search'
  if (n === 'webfetch' || n === 'websearch') return 'public'
  if (n === 'task' || n === 'agent') return 'account_tree'
  if (n === 'todowrite') return 'checklist'
  if (n.startsWith('mcp__')) return 'extension'
  return 'build'
}
