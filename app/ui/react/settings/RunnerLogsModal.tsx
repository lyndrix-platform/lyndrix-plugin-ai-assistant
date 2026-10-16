import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { assistantApi } from '../lib/api'
import { Button, ErrorBox, Modal, Spinner } from '../shared'

const TAILS = [200, 500, 1000, 2000]

/** docker logs of the runner container (admin). Polls while open. */
export default function RunnerLogsModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation('ai_assistant')
  const [tail, setTail] = useState(200)
  const [logs, setLogs] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(true)
  const preRef = useRef<HTMLPreElement>(null)

  const load = useCallback(async () => {
    try {
      const r = await assistantApi.runnerLogs(tail)
      setLogs(r.logs ?? '')
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [tail])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!live) return
    const id = window.setInterval(() => { if (!document.hidden) void load() }, 3000)
    return () => window.clearInterval(id)
  }, [live, load])
  useEffect(() => {
    const el = preRef.current
    if (el && live) el.scrollTop = el.scrollHeight
  }, [logs, live])

  return (
    <Modal title={t('logs.title', { defaultValue: 'Runner logs' })} onClose={onClose} width={960}
      footer={<Button label={t('common.close', { defaultValue: 'Close' })} onClick={onClose} />}>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.6rem', flexWrap: 'wrap' }}>
        <label style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
          {t('logs.tail', { defaultValue: 'Lines' })}
          <select className="lx-select" value={tail} onChange={(e) => setTail(Number(e.target.value))} style={{ width: 'auto', padding: '0.2rem 0.5rem' }}>
            {TAILS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <Button icon={live ? 'pause' : 'play_arrow'} label={live ? t('logs.pause', { defaultValue: 'Pause' }) : t('logs.resume', { defaultValue: 'Live' })} onClick={() => setLive((v) => !v)} variant="ghost" />
        <Button icon="refresh" label={t('common.refresh', { defaultValue: 'Refresh' })} onClick={() => void load()} variant="ghost" />
      </div>
      {error && <ErrorBox msg={error} />}
      {logs === null && !error && <Spinner label={t('common.loading', { defaultValue: 'Loading…' })} compact />}
      {logs !== null && (
        <pre ref={preRef} className="lx-log-panel lxai-pre lxai-scroll" style={{ maxHeight: '60vh', minHeight: 200 }}>
          {logs || t('logs.empty', { defaultValue: '(no output)' })}
        </pre>
      )}
    </Modal>
  )
}
