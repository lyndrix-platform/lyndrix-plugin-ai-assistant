import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { assistantApi, isRunnerUnavailable } from '../lib/api'
import type { AssistantStatus, AuthLastResult, AuthStart, AuthVerify } from '../lib/api'
import { runnerIsUp } from '../lib/status'
import { Badge, Button, Card, ErrorBox, Icon, NoticeBox } from '../shared'

const STATUS_POLL_MS = 4000
const EXCHANGE_POLL_MS = 1500
const EXCHANGE_MAX_MS = 180_000

/** "Sign in with Claude": start → open the URL → paste the code → submit.
 *  Admin only; the API rejects everyone else anyway. */
export default function ClaudeLogin({ status, onChanged }: { status: AssistantStatus | null; onChanged: () => void }) {
  const { t } = useTranslation('ai_assistant')
  const [start, setStart] = useState<AuthStart | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState<'start' | 'submit' | 'verify' | null>(null)
  const [verified, setVerified] = useState<AuthVerify | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null)
  const expiresAt = useRef<number | null>(null)

  const authMode = status?.auth_mode ?? 'none'
  const runnerUp = !!status && runnerIsUp(status)

  const poll = useCallback(async () => {
    try {
      const s = await assistantApi.authStatus()
      setPending(!!s.pending)
      setVerified(s.verified ?? null)
      // Bridge: `authenticated` is the detected credential kind ("none" when absent);
      // plugin fallback while the runner is down: the configured auth mode → unknown.
      const a = s.authenticated
      setAuthenticated(s.runner === 'down' ? null : typeof a === 'boolean' ? a : typeof a === 'string' ? a !== 'none' && a !== '' : null)
      if (s.pending && s.auth_id && !start) setStart((prev) => prev ?? { auth_id: s.auth_id!, url: '', expires_in: 0 })
    } catch { /* runner down → status card says so */ }
  }, [start])

  useEffect(() => {
    void poll()
    const id = window.setInterval(() => { if (!document.hidden) void poll() }, STATUS_POLL_MS)
    return () => window.clearInterval(id)
  }, [poll])

  useEffect(() => {
    if (!start || !start.expires_in) { setSecondsLeft(null); return }
    expiresAt.current = Date.now() + start.expires_in * 1000
    const tick = () => setSecondsLeft(Math.max(0, Math.round(((expiresAt.current ?? 0) - Date.now()) / 1000)))
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [start])

  const begin = async () => {
    setBusy('start')
    setError(null)
    setDone(null)
    try {
      const r = await assistantApi.authStart()
      setStart(r)
      setCode('')
      setPending(true)
    } catch (err) {
      setError(isRunnerUnavailable(err) ? t('login.runnerNeeded', { defaultValue: 'The runner must be up to sign in — start or restart it first.' }) : (err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  /** The code is handed over at once (202); the exchange runs in the runner and
   *  the plugin stores the token itself — we only watch /auth/status for the
   *  outcome, so a slow exchange never depends on one long browser request. */
  const submit = async () => {
    if (!start || !code.trim()) return
    setBusy('submit')
    setError(null)
    const authId = start.auth_id
    try {
      await assistantApi.authSubmit(authId, code.trim())
    } catch (err) {
      setError((err as Error).message)
      setBusy(null)
      return
    }
    const startedAt = Date.now()
    const finish = (res: AuthLastResult | null | undefined, authenticated: boolean) => {
      if (res?.phase === 'failed') {
        setError(res.error || t('login.failed', { defaultValue: 'Sign-in failed' }))
      } else {
        setDone(authenticated
          ? t('login.success', { defaultValue: 'Signed in.' })
          : t('login.successRestarting', { defaultValue: 'Signed in. The token is stored in Vault and the runner restarts with it.' }))
        setStart(null)
        setCode('')
        setPending(false)
        onChanged()
      }
      setBusy(null)
    }
    const tick = async () => {
      try {
        const s = await assistantApi.authStatus()
        const res = s.last_result
        const a = s.authenticated
        const authenticated = typeof a === 'boolean' ? a : typeof a === 'string' && a !== 'none' && a !== ''
        if (res && res.auth_id === authId && (res.phase === 'failed' || res.phase === 'done')) { finish(res, authenticated); return }
        if (authenticated && res?.phase === 'storing') { finish(res, true); return }
      } catch { /* runner restarting with the fresh token → keep polling */ }
      if (Date.now() - startedAt > EXCHANGE_MAX_MS) {
        setError(t('login.exchangeTimeout', { defaultValue: 'No outcome from the runner within 3 minutes — check the runner log and start again.' }))
        setBusy(null)
        return
      }
      window.setTimeout(() => { void tick() }, EXCHANGE_POLL_MS)
    }
    window.setTimeout(() => { void tick() }, EXCHANGE_POLL_MS)
  }

  const cancel = () => { setStart(null); setCode(''); setError(null) }

  /** One tiny headless turn in the runner — the only proof the credential works. */
  const verify = async () => {
    setBusy('verify')
    setError(null)
    try {
      const v = await assistantApi.authVerify()
      setVerified(v)
      if (!v.ok) setError(t('login.verifyFailed', { defaultValue: 'The credential does not work: {{error}}', error: v.error || '?' }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const modeLabel = authMode === 'oauth'
    ? t('login.modeOauth', { defaultValue: 'Claude subscription (OAuth token)' })
    : authMode === 'api_key'
      ? t('login.modeApiKey', { defaultValue: 'Anthropic API key' })
      : t('login.modeNone', { defaultValue: 'not signed in' })
  const tone = authMode === 'none' ? 'down' : 'up'

  return (
    <Card title={t('login.title', { defaultValue: 'Claude sign-in' })} icon="key"
      actions={<Badge tone={tone} label={modeLabel} />}>
      <p style={{ margin: 0, fontSize: '0.78rem', color: 'var(--lx-text-muted)' }}>
        {t('login.body', { defaultValue: 'Sign in with a Claude Pro/Max subscription: the runner opens a Claude login, you confirm it in the browser and paste the code shown there. The resulting long-lived token is stored in Vault (plugin setting claude_oauth_token). An API key can be stored in the plugin settings instead.' })}
      </p>
      {authenticated === true && authMode !== 'none' && !start && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.78rem', flexWrap: 'wrap',
          color: verified?.ok ? 'var(--lx-state-up)' : verified && !verified.ok ? 'var(--lx-state-down)' : 'var(--lx-text-muted)' }}>
          <Icon name={verified?.ok ? 'verified' : verified && !verified.ok ? 'error_outline' : 'key'} size={16} />
          {verified?.ok
            ? t('login.verifiedOk', { defaultValue: 'Verified: the credential works (checked {{at}}).', at: verified.at ? new Date(verified.at).toLocaleTimeString() : '' })
            : verified && !verified.ok
              ? t('login.verifiedBad', { defaultValue: 'A credential is configured but it does not work — sign in again.' })
              : t('login.runnerConfigured', { defaultValue: 'A credential is configured in the runner (not verified yet).' })}
          <Button icon="fact_check" label={t('login.verify', { defaultValue: 'Verify login' })} onClick={() => void verify()} busy={busy === 'verify'} disabled={!runnerUp} />
        </div>
      )}
      {authenticated === false && authMode !== 'none' && !start && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.78rem', color: 'var(--lx-state-paused)' }}>
          <Icon name="warning_amber" size={16} />{t('login.runnerNotAuthenticated', { defaultValue: 'A credential is configured but the runner does not report a valid login — the token may have expired.' })}
        </div>
      )}
      {error && <ErrorBox msg={error} onDismiss={() => setError(null)} />}
      {done && <NoticeBox msg={done} />}

      {!start ? (
        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
          <Button variant="primary" icon="login" label={authMode === 'none' ? t('login.signIn', { defaultValue: 'Sign in with Claude' }) : t('login.signInAgain', { defaultValue: 'Sign in again' })}
            onClick={() => void begin()} busy={busy === 'start'} disabled={!runnerUp} title={!runnerUp ? t('login.runnerNeeded', { defaultValue: 'The runner must be up to sign in — start or restart it first.' }) : undefined} />
          {pending && <span style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{t('login.pendingHint', { defaultValue: 'A sign-in is already pending in the runner; starting again replaces it.' })}</span>}
        </div>
      ) : (
        <div className="lx-elevated" style={{ padding: '0.8rem 0.9rem', display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
          <ol style={{ margin: 0, paddingLeft: '1.2rem', fontSize: '0.8rem', color: 'var(--lx-text)', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            <li>
              {t('login.step1', { defaultValue: 'Open the Claude login page and confirm the access:' })}{' '}
              {start.url ? (
                <a href={start.url} target="_blank" rel="noreferrer noopener" className="lx-btn lx-btn--secondary lx-btn--sm" style={{ textDecoration: 'none', marginLeft: 4 }}>
                  <Icon name="open_in_new" size={14} />{t('login.openUrl', { defaultValue: 'Open login page' })}
                </a>
              ) : (
                <span style={{ color: 'var(--lx-text-muted)' }}>{t('login.urlUnknown', { defaultValue: '(URL not available — start the sign-in again to get a fresh link)' })}</span>
              )}
              {start.url && (
                <div className="lx-mono" style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', wordBreak: 'break-all', marginTop: 4 }}>{start.url}</div>
              )}
            </li>
            <li>
              {t('login.step2', { defaultValue: 'Paste the code shown after the confirmation:' })}
              <div style={{ display: 'flex', gap: '0.5rem', marginTop: 6, flexWrap: 'wrap' }}>
                <input className="lx-input lx-mono" value={code} onChange={(e) => setCode(e.target.value)} placeholder={t('login.codePlaceholder', { defaultValue: 'authorization code' })}
                  style={{ flex: 1, minWidth: 220 }} autoComplete="off" spellCheck={false}
                  onKeyDown={(e) => { if (e.key === 'Enter') void submit() }} />
                <Button variant="primary" icon="check" label={t('login.submit', { defaultValue: 'Submit code' })} onClick={() => void submit()} disabled={!code.trim()} busy={busy === 'submit'} />
                <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={cancel} disabled={busy !== null} />
              </div>
              {busy === 'submit' && (
                <div style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', marginTop: 6 }}>
                  {t('login.exchanging', { defaultValue: 'The runner exchanges the code for a token and stores it — this can take up to two minutes.' })}
                </div>
              )}
            </li>
          </ol>
          {secondsLeft !== null && (
            <span style={{ fontSize: '0.7rem', color: secondsLeft === 0 ? 'var(--lx-state-down)' : 'var(--lx-text-muted)' }}>
              {secondsLeft === 0
                ? t('login.expired', { defaultValue: 'The sign-in request expired — start again.' })
                : t('login.expiresIn', { defaultValue: 'Expires in {{seconds}} s', seconds: secondsLeft })}
            </span>
          )}
        </div>
      )}
    </Card>
  )
}
