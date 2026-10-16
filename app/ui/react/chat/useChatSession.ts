/**
 * One open chat session: persisted history + live turn.
 *
 * Lifecycle
 *  - load:   GET /sessions/{id} → items from the DB; if the session is running
 *            (DB status or bridge live state) attach to the event stream.
 *  - send:   optimistic user item → POST messages → stream from the last seq.
 *  - stream: fetch-based SSE (lib/sse.ts); every event goes through
 *            model.applyEvent. Reconnects with after=<last seq> on drop while
 *            the turn is running; stops on result/error.
 *  - end:    after result/error the persisted transcript is re-read and
 *            replaces the live items once the recorder has written the turn
 *            (the DB is the source of truth for finished turns, which also
 *            clears any duplicates a replayed catch-up stream produced).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { assistantApi, isRunnerUnavailable } from '../lib/api'
import type { ApprovalDecision, SessionOut, StreamEvent } from '../lib/api'
import { openEventStream } from '../lib/sse'
import type { StreamHandle } from '../lib/sse'
import { applyEvent, isTurnEnd, itemsFromMessages, lastTurnBoundary, liveState, seqHint, settleApprovals } from './model'
import type { Phase, TranscriptItem } from './model'

const RECONNECT_MAX = 8
const IDLE_GRACE_MS = 3000
const RECONCILE_DELAY_MS = 700
const RECONCILE_RETRY_MS = 2500

/** Sentinel error code the view translates (not a server message). */
export const STREAM_LOST = '__stream_lost__'

export interface ChatSessionState {
  session: SessionOut | null
  items: TranscriptItem[]
  phase: Phase
  loading: boolean
  error: string | null
  /** The API answered 503 — runner down or pinned to another instance. */
  runnerUnavailable: boolean
  streaming: boolean
  send: (text: string) => Promise<void>
  approve: (approvalId: string, decision: ApprovalDecision) => Promise<void>
  interrupt: () => Promise<void>
  reload: () => Promise<void>
  dismissError: () => void
}

export function useChatSession(sessionId: string | null, onSessionChanged?: (s: SessionOut) => void): ChatSessionState {
  const [session, setSessionState] = useState<SessionOut | null>(null)
  const [items, setItemsState] = useState<TranscriptItem[]>([])
  const [phase, setPhaseState] = useState<Phase>('idle')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runnerUnavailable, setRunnerUnavailable] = useState(false)
  const [streaming, setStreaming] = useState(false)

  // Refs mirror the state the stream callbacks need synchronously.
  const itemsRef = useRef<TranscriptItem[]>([])
  const sessionRef = useRef<SessionOut | null>(null)
  const phaseRef = useRef<Phase>('idle')
  const streamRef = useRef<StreamHandle | null>(null)
  const lastSeqRef = useRef(0)
  /** Catch-up with a turn that was already running when the session was opened:
   *  the bridge replays its whole ring buffer from seq 1, so events up to the
   *  snapshot's `last_seq` are buffered and cut at the last turn boundary. */
  const catchUpRef = useRef<{ limit: number; buffer: StreamEvent[] } | null>(null)
  const reconnectsRef = useRef(0)
  const timersRef = useRef<number[]>([])
  const sessionIdRef = useRef<string | null>(sessionId)
  const onChangedRef = useRef(onSessionChanged)
  onChangedRef.current = onSessionChanged

  const commitItems = (next: TranscriptItem[]) => { itemsRef.current = next; setItemsState(next) }
  const setSession = (next: SessionOut | null) => { sessionRef.current = next; setSessionState(next) }
  const setPhase = (p: Phase) => { phaseRef.current = p; setPhaseState(p) }

  const clearTimers = () => {
    for (const t of timersRef.current) window.clearTimeout(t)
    timersRef.current = []
  }
  const later = (fn: () => void, ms: number) => {
    const t = window.setTimeout(() => {
      timersRef.current = timersRef.current.filter((x) => x !== t)
      fn()
    }, ms)
    timersRef.current.push(t)
  }

  const stopStream = useCallback(() => {
    streamRef.current?.close()
    streamRef.current = null
    setStreaming(false)
  }, [])

  const failWith = (err: unknown) => {
    if (isRunnerUnavailable(err)) setRunnerUnavailable(true)
    setError(err instanceof Error ? err.message : String(err))
  }

  /** Re-read the persisted transcript; replace the live items once the turn is in the DB. */
  const reconcile = useCallback(async (sid: string, attempt = 0, force = false) => {
    try {
      const detail = await assistantApi.getSession(sid)
      if (sessionIdRef.current !== sid) return
      setSession(detail.session)
      onChangedRef.current?.(detail.session)
      const hint = seqHint(detail.live)
      if (hint !== null && hint > lastSeqRef.current) lastSeqRef.current = hint
      const persisted = itemsFromMessages(detail.messages)
      const last = persisted[persisted.length - 1]
      // The recorder appends the assistant text + a result row when the turn
      // ends; until then the history still ends with the prompt or a tool card.
      const turnPersisted = !!last && (isTurnEnd(last) || last.kind === 'assistant')
      if (force || turnPersisted) commitItems(persisted)
      else if (attempt < 1) later(() => void reconcile(sid, attempt + 1), RECONCILE_RETRY_MS)
      // else: keep the live items (DB not ready / recorder lagging) — nothing is lost.
    } catch (err) {
      if (sessionIdRef.current !== sid) return
      if (attempt < 1) later(() => void reconcile(sid, attempt + 1, force), RECONCILE_RETRY_MS)
      else failWith(err)
    }
  }, [])

  const finishTurn = useCallback((sid: string) => {
    stopStream()
    clearTimers()
    setPhase('idle')
    commitItems(settleApprovals(itemsRef.current))
    later(() => void reconcile(sid), RECONCILE_DELAY_MS)
  }, [reconcile, stopStream])

  const startStream = useCallback((sid: string) => {
    if (sessionIdRef.current !== sid) return
    stopStream()
    setStreaming(true)

    const scheduleReconnect = () => {
      reconnectsRef.current += 1
      if (reconnectsRef.current > RECONNECT_MAX) {
        setStreaming(false)
        setError(STREAM_LOST)
        setPhase('idle')
        later(() => void reconcile(sid, 0, true), 500)
        return
      }
      const delay = Math.min(5000, 500 * 2 ** (reconnectsRef.current - 1))
      later(() => { if (sessionIdRef.current === sid && phaseRef.current !== 'idle' && phaseRef.current !== 'closed') startStream(sid) }, delay)
    }

    /** Normal path for one event: reducer → phase → turn end. Returns true when the turn ended. */
    const process = (ev: StreamEvent): boolean => {
      const r = applyEvent(itemsRef.current, ev)
      commitItems(r.items)
      if (r.turnEnded) {
        finishTurn(sid)
        return true
      }
      if (r.phase && r.phase !== 'idle') setPhase(r.phase)
      const state = typeof ev.data?.state === 'string' ? ev.data.state : null
      if (ev.type === 'status' && (state === 'idle' || state === 'closed' || state === 'done')) {
        // A result normally follows; if it does not, close the turn after a grace period.
        later(() => { if (phaseRef.current !== 'idle' && sessionIdRef.current === sid) finishTurn(sid) }, IDLE_GRACE_MS)
      }
      return false
    }

    /** Apply the buffered replay: only what follows the last finished turn. */
    const flushCatchUp = (): boolean => {
      const cu = catchUpRef.current
      catchUpRef.current = null
      if (!cu || cu.buffer.length === 0) return false
      const cut = lastTurnBoundary(cu.buffer)
      if (cut === cu.buffer.length - 1) {
        // The turn we were catching up with has already ended — nothing live to show.
        finishTurn(sid)
        return true
      }
      for (const ev of cu.buffer.slice(cut + 1)) {
        if (process(ev)) return true
      }
      return false
    }

    const handle = openEventStream(assistantApi.eventsUrl(sid, lastSeqRef.current), {
      onEvent: (ev: StreamEvent) => {
        if (sessionIdRef.current !== sid || streamRef.current !== handle) return
        if (ev.seq > lastSeqRef.current) lastSeqRef.current = ev.seq
        reconnectsRef.current = 0
        const cu = catchUpRef.current
        if (cu) {
          if (ev.seq <= cu.limit) {
            cu.buffer.push(ev)
            if (ev.seq === cu.limit) flushCatchUp()
            return
          }
          if (flushCatchUp()) return
        }
        process(ev)
      },
      onEnd: () => {
        if (sessionIdRef.current !== sid || streamRef.current !== handle) return
        if (phaseRef.current === 'idle' || phaseRef.current === 'closed') { setStreaming(false); return }
        scheduleReconnect()
      },
      onError: (err) => {
        if (sessionIdRef.current !== sid || streamRef.current !== handle) return
        if (isRunnerUnavailable(err)) {
          setRunnerUnavailable(true)
          setError(err.message)
          setStreaming(false)
          setPhase('idle')
          return
        }
        if (phaseRef.current === 'idle' || phaseRef.current === 'closed') { setStreaming(false); return }
        scheduleReconnect()
      },
    })
    streamRef.current = handle
  }, [finishTurn, reconcile, stopStream])

  // ── load on session change ─────────────────────────────────────────────────
  useEffect(() => {
    sessionIdRef.current = sessionId
    stopStream()
    clearTimers()
    reconnectsRef.current = 0
    lastSeqRef.current = 0
    catchUpRef.current = null
    commitItems([])
    setSession(null)
    setError(null)
    setPhase('idle')
    if (!sessionId) return
    let alive = true
    setLoading(true)
    assistantApi.getSession(sessionId)
      .then((detail) => {
        if (!alive) return
        setSession(detail.session)
        commitItems(itemsFromMessages(detail.messages))
        const hint = seqHint(detail.live)
        if (hint !== null) lastSeqRef.current = hint
        const st = liveState(detail.live) ?? detail.session.status
        if (detail.session.status === 'closed' || detail.session.archived) {
          setPhase('closed')
        } else if (st === 'running' || st === 'waiting_approval' || st === 'busy') {
          setPhase(st === 'waiting_approval' ? 'waiting_approval' : 'running')
          // Catch up with the running turn: replay from the start of the ring
          // buffer, keep only what follows the last turn boundary (see
          // catchUpRef). Without a seq hint everything is applied; tool and
          // approval items merge by id and the transcript is reconciled with
          // the DB when the turn ends.
          if (hint !== null && hint > 0) {
            catchUpRef.current = { limit: hint, buffer: [] }
            lastSeqRef.current = 0
          }
          startStream(sessionId)
        } else {
          setPhase('idle')
        }
        setRunnerUnavailable(false)
      })
      .catch((err) => { if (alive) failWith(err) })
      .finally(() => { if (alive) setLoading(false) })
    return () => {
      alive = false
      stopStream()
      clearTimers()
    }
  }, [sessionId, startStream, stopStream])

  // ── actions ────────────────────────────────────────────────────────────────
  const send = useCallback(async (text: string) => {
    const sid = sessionIdRef.current
    const content = text.trim()
    if (!sid || !content) return
    setError(null)
    const tempId = `tmp-${Date.now()}`
    commitItems([...itemsRef.current, { id: tempId, kind: 'user', text: content, at: new Date().toISOString(), pending: true }])
    setPhase('running')
    try {
      // Fresh seq hint first: the ring buffer still holds the tail of the previous
      // turn (result / status idle), which must not be replayed into this one.
      try {
        const snap = await assistantApi.getSession(sid)
        const h = seqHint(snap.live)
        if (h !== null && h > lastSeqRef.current) lastSeqRef.current = h
      } catch { /* best effort — the reducer tolerates a replay */ }
      if (sessionIdRef.current !== sid) return
      await assistantApi.sendMessage(sid, content)
      if (sessionIdRef.current !== sid) return
      commitItems(itemsRef.current.map((it) => (it.id === tempId && it.kind === 'user' ? { ...it, pending: false } : it)))
      setRunnerUnavailable(false)
      reconnectsRef.current = 0
      startStream(sid)
      if (sessionRef.current) {
        const next: SessionOut = { ...sessionRef.current, status: 'running', last_activity: new Date().toISOString() }
        setSession(next)
        onChangedRef.current?.(next)
      }
    } catch (err) {
      if (sessionIdRef.current !== sid) return
      commitItems(itemsRef.current.filter((it) => it.id !== tempId))
      setPhase('idle')
      failWith(err)
    }
  }, [startStream])

  const approve = useCallback(async (approvalId: string, decision: ApprovalDecision) => {
    const sid = sessionIdRef.current
    if (!sid) return
    setError(null)
    try {
      await assistantApi.approve(sid, approvalId, decision)
      if (sessionIdRef.current !== sid) return
      commitItems(itemsRef.current.map((it) => (it.kind === 'approval' && it.approvalId === approvalId ? { ...it, decision } : it)))
      setPhase('running')
    } catch (err) {
      failWith(err)
    }
  }, [])

  const interrupt = useCallback(async () => {
    const sid = sessionIdRef.current
    if (!sid) return
    try {
      await assistantApi.interrupt(sid)
      // The bridge answers with result/error on the stream; if it does not, fall back.
      later(() => { if (phaseRef.current !== 'idle' && sessionIdRef.current === sid) finishTurn(sid) }, IDLE_GRACE_MS * 2)
    } catch (err) {
      failWith(err)
    }
  }, [finishTurn])

  const reload = useCallback(async () => {
    const sid = sessionIdRef.current
    if (!sid) return
    setLoading(true)
    try {
      await reconcile(sid, 1, true)
    } finally {
      setLoading(false)
    }
  }, [reconcile])

  return {
    session, items, phase, loading, error, runnerUnavailable, streaming,
    send, approve, interrupt, reload,
    dismissError: () => setError(null),
  }
}
