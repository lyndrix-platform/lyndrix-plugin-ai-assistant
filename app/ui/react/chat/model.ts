/**
 * Transcript model: pure functions that turn the persisted history
 * (GET /sessions/{id}.messages) and the live SSE events into one list of
 * renderable items. No React, no I/O — unit-testable in isolation.
 */
import type { MessageOut, StreamEvent } from '../lib/api'

export type TranscriptItem =
  | { id: string; kind: 'user'; text: string; at: string | null; pending?: boolean }
  | { id: string; kind: 'assistant'; text: string; streaming: boolean }
  | { id: string; kind: 'thinking'; text: string }
  | { id: string; kind: 'tool'; toolUseId: string; name: string; input: unknown; result?: { output: string; isError: boolean } }
  | { id: string; kind: 'approval'; approvalId: string; toolUseId: string | null; name: string; input: unknown; decision: string | null }
  | { id: string; kind: 'system'; text: string; tone: 'muted' | 'error' }
  | { id: string; kind: 'result'; costUsd: number | null; durationMs: number | null; numTurns: number | null; isError: boolean; text: string | null }

export type Phase = 'idle' | 'running' | 'waiting_approval' | 'closed'

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() && !Number.isNaN(Number(v)) ? Number(v) : null)
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : v === null || v === undefined ? fallback : String(v))

// ─── Persisted history ──────────────────────────────────────────────────────

export function itemsFromMessages(messages: MessageOut[]): TranscriptItem[] {
  const items: TranscriptItem[] = []
  for (const m of messages) {
    const id = `db-${m.id}`
    const meta = m.meta ?? {}
    if (m.kind === 'text' && m.role === 'user') {
      items.push({ id, kind: 'user', text: m.content ?? '', at: m.created_at })
    } else if (m.kind === 'text') {
      items.push({ id, kind: 'assistant', text: m.content ?? '', streaming: false })
    } else if (m.kind === 'tool_use') {
      items.push({ id, kind: 'tool', toolUseId: str(meta.tool_use_id, id), name: str(meta.name, 'tool'), input: meta.input })
    } else if (m.kind === 'tool_result') {
      const tid = str(meta.tool_use_id)
      const target = tid ? findLastTool(items, tid) : null
      const result = { output: m.content ?? '', isError: !!meta.is_error }
      if (target) target.result = result
      else items.push({ id, kind: 'tool', toolUseId: tid || id, name: str(meta.name, 'tool'), input: undefined, result })
    } else if (m.kind === 'approval') {
      items.push({ id, kind: 'system', text: `approval:${str(meta.decision, 'unknown')}`, tone: 'muted' })
    } else if (m.kind === 'result') {
      items.push({
        id, kind: 'result',
        costUsd: num(meta.total_cost_usd), durationMs: num(meta.duration_ms), numTurns: num(meta.num_turns),
        isError: !!meta.is_error, text: meta.is_error && typeof meta.result === 'string' ? meta.result : null,
      })
    } else if (m.kind === 'error') {
      items.push({ id, kind: 'system', text: m.content ?? 'error', tone: 'error' })
    } else if (m.content) {
      items.push({ id, kind: 'system', text: m.content, tone: 'muted' })
    }
  }
  return items
}

function findLastTool(items: TranscriptItem[], toolUseId: string): Extract<TranscriptItem, { kind: 'tool' }> | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'tool' && it.toolUseId === toolUseId) return it
  }
  return null
}

function findApproval(items: TranscriptItem[], approvalId: string): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'approval' && it.approvalId === approvalId) return i
  }
  return -1
}

/** True when the item is a turn boundary (result/error of a finished turn). */
export const isTurnEnd = (it: TranscriptItem) => it.kind === 'result' || (it.kind === 'system' && it.tone === 'error')

// ─── Live events ────────────────────────────────────────────────────────────

export interface ApplyResult {
  items: TranscriptItem[]
  phase: Phase | null      // null → unchanged
  turnEnded: boolean       // result/error of the running turn arrived
}

/** Applies one SSE event. Returns a new array (never mutates the input). */
export function applyEvent(prev: TranscriptItem[], ev: StreamEvent): ApplyResult {
  const items = prev.slice()
  const d = ev.data ?? {}
  const id = `ev-${ev.seq}-${ev.type}`
  let phase: Phase | null = null
  let turnEnded = false

  const settleStreaming = () => {
    const last = items[items.length - 1]
    if (last && last.kind === 'assistant' && last.streaming) items[items.length - 1] = { ...last, streaming: false }
  }

  switch (ev.type) {
    case 'status': {
      const state = str(d.state)
      if (state === 'waiting_approval') phase = 'waiting_approval'
      else if (state === 'running' || state === 'busy') phase = 'running'
      // `idle` is handled by the hook with a grace timer (a result normally follows).
      break
    }
    case 'assistant_text': {
      const text = str(d.text)
      const last = items[items.length - 1]
      if (last && last.kind === 'assistant' && last.streaming) {
        items[items.length - 1] = { ...last, text: d.delta ? last.text + text : text }
      } else {
        items.push({ id, kind: 'assistant', text, streaming: true })
      }
      phase = 'running'
      break
    }
    case 'thinking': {
      const text = str(d.text)
      const last = items[items.length - 1]
      if (last && last.kind === 'thinking') items[items.length - 1] = { ...last, text: d.delta === false ? text : last.text + text }
      else if (text.trim()) items.push({ id, kind: 'thinking', text })
      break
    }
    case 'tool_use': {
      settleStreaming()
      const toolUseId = str(d.tool_use_id, id)
      const name = str(d.name, 'tool')
      const idx = items.findIndex((it) => it.kind === 'tool' && it.toolUseId === toolUseId)
      const existing = idx >= 0 ? (items[idx] as Extract<TranscriptItem, { kind: 'tool' }>) : null
      const item: TranscriptItem = { id: existing?.id ?? id, kind: 'tool', toolUseId, name, input: d.input, result: existing?.result }
      if (idx >= 0) items[idx] = item
      else items.push(item)
      phase = 'running'
      break
    }
    case 'tool_result': {
      const toolUseId = str(d.tool_use_id)
      const result = { output: str(d.output ?? d.content), isError: !!d.is_error }
      const idx = toolUseId ? items.findIndex((it) => it.kind === 'tool' && it.toolUseId === toolUseId) : -1
      if (idx >= 0) items[idx] = { ...(items[idx] as Extract<TranscriptItem, { kind: 'tool' }>), result }
      else items.push({ id, kind: 'tool', toolUseId: toolUseId || id, name: str(d.name, 'tool'), input: undefined, result })
      break
    }
    case 'permission_request': {
      settleStreaming()
      const approvalId = str(d.approval_id, id)
      const idx = findApproval(items, approvalId)
      const item: TranscriptItem = {
        id: idx >= 0 ? items[idx].id : id, kind: 'approval', approvalId,
        toolUseId: d.tool_use_id ? str(d.tool_use_id) : null, name: str(d.name, 'tool'), input: d.input,
        decision: idx >= 0 ? (items[idx] as Extract<TranscriptItem, { kind: 'approval' }>).decision : null,
      }
      if (idx >= 0) items[idx] = item
      else items.push(item)
      phase = 'waiting_approval'
      break
    }
    case 'permission_resolved': {
      const approvalId = str(d.approval_id)
      const idx = approvalId ? findApproval(items, approvalId) : -1
      if (idx >= 0) items[idx] = { ...(items[idx] as Extract<TranscriptItem, { kind: 'approval' }>), decision: str(d.decision, 'resolved') }
      else if (d.auto) items.push({ id, kind: 'system', text: `auto:${str(d.decision, 'deny')}:${str(d.name, 'tool')}`, tone: 'muted' })
      phase = 'running'
      break
    }
    case 'result': {
      settleStreaming()
      items.push({
        id, kind: 'result',
        costUsd: num(d.total_cost_usd), durationMs: num(d.duration_ms), numTurns: num(d.num_turns), isError: !!d.is_error,
        text: d.is_error && typeof d.result === 'string' ? d.result : null,
      })
      phase = 'idle'
      turnEnded = true
      break
    }
    case 'error': {
      settleStreaming()
      items.push({ id, kind: 'system', text: str(d.message, 'error'), tone: 'error' })
      phase = 'idle'
      turnEnded = true
      break
    }
    default:
      break
  }
  return { items, phase, turnEnded }
}

/** Index after which the *current* turn starts in a replayed event list: the
 *  bridge emits `status idle` when a turn ends (and `result` just before), so
 *  everything up to the last such boundary belongs to finished turns that the
 *  persisted history already covers. -1 → no boundary found (apply everything). */
export function lastTurnBoundary(events: StreamEvent[]): number {
  let boundary = -1
  let lastResult = -1
  events.forEach((ev, i) => {
    if (ev.type === 'status' && (ev.data?.state === 'idle' || ev.data?.state === 'closed')) boundary = i
    if (ev.type === 'result') lastResult = i
  })
  return boundary >= 0 ? boundary : lastResult
}

/** Mark every approval without a decision as resolved (after a turn ended). */
export function settleApprovals(items: TranscriptItem[], decision = 'expired'): TranscriptItem[] {
  return items.map((it) => (it.kind === 'approval' && it.decision === null ? { ...it, decision } : it))
}

/** Pending approvals, newest first. */
export function pendingApprovals(items: TranscriptItem[]): Extract<TranscriptItem, { kind: 'approval' }>[] {
  return items.filter((it): it is Extract<TranscriptItem, { kind: 'approval' }> => it.kind === 'approval' && it.decision === null).reverse()
}

/** Seq hint of the bridge's live session view, if it exposes one. */
export function seqHint(live: Record<string, unknown> | null | undefined): number | null {
  if (!live) return null
  for (const key of ['last_seq', 'seq', 'event_seq', 'last_event_seq', 'events_seq']) {
    const v = num(live[key])
    if (v !== null) return v
  }
  const nested = live.events
  if (Array.isArray(nested) && nested.length) {
    const last = nested[nested.length - 1] as Record<string, unknown>
    const v = num(last?.seq)
    if (v !== null) return v
  }
  return null
}

/** Bridge-side state of the session ("running" | "waiting_approval" | "idle" | …) if exposed. */
export function liveState(live: Record<string, unknown> | null | undefined): string | null {
  if (!live) return null
  const v = live.state ?? live.status
  return typeof v === 'string' ? v : null
}
