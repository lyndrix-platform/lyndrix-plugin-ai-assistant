/**
 * Minimal SSE reader on top of fetch.
 *
 * EventSource cannot send the Authorization header the plugin API requires, so
 * the stream is read manually: ReadableStream → text → frames split on blank
 * lines → `data:` lines → JSON ({seq, ts, type, data}). Reconnects are the
 * caller's job (it knows the last seq it applied).
 */
import { ApiError, authHeaders } from './api'
import type { StreamEvent } from './api'

export interface StreamHandlers {
  onEvent: (ev: StreamEvent) => void
  /** Transport/HTTP error — the stream is closed afterwards. */
  onError: (err: Error) => void
  /** Server closed the stream normally (no error). */
  onEnd: () => void
}

export interface StreamHandle {
  close: () => void
  readonly closed: boolean
}

function parseFrame(frame: string): StreamEvent | null {
  const dataLines: string[] = []
  for (const raw of frame.split(/\r?\n/)) {
    if (raw.startsWith('data:')) dataLines.push(raw.slice(5).replace(/^ /, ''))
    // `event:`/`id:`/comments are ignored — the bridge puts everything in data.
  }
  if (dataLines.length === 0) return null
  try {
    const parsed = JSON.parse(dataLines.join('\n')) as Partial<StreamEvent>
    if (!parsed || typeof parsed !== 'object' || typeof parsed.type !== 'string') return null
    return {
      seq: typeof parsed.seq === 'number' ? parsed.seq : Number(parsed.seq) || 0,
      ts: parsed.ts,
      type: parsed.type,
      data: (parsed.data && typeof parsed.data === 'object' ? parsed.data : {}) as Record<string, unknown>,
    }
  } catch {
    return null
  }
}

export function openEventStream(url: string, handlers: StreamHandlers): StreamHandle {
  const controller = new AbortController()
  let closed = false

  const finish = (fn: () => void) => {
    if (closed) return
    closed = true
    fn()
  }

  ;(async () => {
    let res: Response
    try {
      res = await fetch(url, {
        headers: { Accept: 'text/event-stream', ...authHeaders() },
        signal: controller.signal,
        cache: 'no-store',
      })
    } catch (err) {
      if (controller.signal.aborted) return
      finish(() => handlers.onError(err instanceof Error ? err : new Error(String(err))))
      return
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = (await res.json()) as { detail?: unknown }
        if (typeof body.detail === 'string') detail = body.detail
      } catch { /* ignore */ }
      finish(() => handlers.onError(new ApiError(res.status, detail)))
      return
    }
    if (!res.body) {
      finish(() => handlers.onError(new Error('Streaming not supported by this browser')))
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        // A frame ends with a blank line (\n\n or \r\n\r\n).
        while ((idx = buffer.search(/\r?\n\r?\n/)) >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx).replace(/^\r?\n\r?\n/, '')
          const ev = parseFrame(frame)
          if (ev && !closed) handlers.onEvent(ev)
        }
      }
      buffer += decoder.decode()
      if (buffer.trim()) {
        const ev = parseFrame(buffer)
        if (ev && !closed) handlers.onEvent(ev)
      }
      finish(() => handlers.onEnd())
    } catch (err) {
      if (controller.signal.aborted) return
      finish(() => handlers.onError(err instanceof Error ? err : new Error(String(err))))
    }
  })()

  return {
    close: () => {
      closed = true
      controller.abort()
    },
    get closed() {
      return closed
    },
  }
}
