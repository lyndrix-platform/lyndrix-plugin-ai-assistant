import React, { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { assistantApi } from './lib/api'
import type { AssistantStatus } from './lib/api'
import { fmtDateTime } from './lib/format'
import { Badge, Button, Card, ErrorBox, Icon, InfoBox, KV, NoticeBox, PageHeader, Spinner, goChat, useAccess, useNotice } from './shared'
import type { Tone } from './shared'
import ClaudeLogin from './settings/ClaudeLogin'
import RunnerLogsModal from './settings/RunnerLogsModal'
import { runnerIsUp } from './lib/status'

function runnerTone(status: AssistantStatus | null): Tone {
  if (!status) return 'unknown'
  const s = String(status.runner?.status ?? '')
  if (s === 'starting') return 'paused'
  if (s === 'unconfigured') return 'paused'
  return runnerIsUp(status) ? 'up' : 'down'
}

function authSummary(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'ok' : 'missing'
  if (typeof v === 'string') return v
  if (typeof v === 'object') {
    const o = v as Record<string, unknown>
    const parts = Object.entries(o).filter(([, val]) => typeof val !== 'object').map(([k, val]) => `${k}: ${String(val)}`)
    return parts.join(', ') || JSON.stringify(v)
  }
  return String(v)
}

/** /ai/settings — status, Claude sign-in, maintenance actions, instruction preview. */
export default function SettingsView() {
  const { t, i18n } = useTranslation('ai_assistant')
  const access = useAccess()
  const [status, setStatus] = useState<AssistantStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useNotice(6000)
  const [busy, setBusy] = useState<'sync' | 'restart' | null>(null)
  const [showLogs, setShowLogs] = useState(false)
  const [instructions, setInstructions] = useState<string | null>(null)
  const [instructionsOpen, setInstructionsOpen] = useState(false)
  const [instructionsError, setInstructionsError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setStatus(await assistantApi.status())
      setError(null)
    } catch (err) {
      setError(`${t('msg.statusFailed', { defaultValue: 'Could not load the status' })}: ${(err as Error).message}`)
    }
  }, [t])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const id = window.setInterval(() => { if (!document.hidden) void load() }, 15_000)
    return () => window.clearInterval(id)
  }, [load])

  const loadInstructions = useCallback(async () => {
    try {
      const r = await assistantApi.instructions()
      setInstructions(r.markdown ?? '')
      setInstructionsError(null)
    } catch (err) {
      setInstructionsError((err as Error).message)
    }
  }, [])

  useEffect(() => { if (instructionsOpen && instructions === null) void loadInstructions() }, [instructionsOpen, instructions, loadInstructions])

  const sync = async () => {
    setBusy('sync')
    try {
      const r = await assistantApi.syncWorkspace()
      if (r.ok === false) setError(`${t('msg.syncFailed', { defaultValue: 'Workspace sync failed' })}: ${r.error ?? '?'}`)
      else setNotice(t('msg.synced', { defaultValue: 'Workspace synchronised and instructions regenerated.' }))
      setInstructions(null)
      await load()
    } catch (err) {
      setError(`${t('msg.syncFailed', { defaultValue: 'Workspace sync failed' })}: ${(err as Error).message}`)
    } finally {
      setBusy(null)
    }
  }

  const restart = async () => {
    setBusy('restart')
    try {
      const r = await assistantApi.restartRunner()
      const st = String(r.runner?.status ?? '')
      if (['error', 'unreachable', 'unconfigured', 'not_bound'].includes(st)) {
        setError(`${t('msg.restartFailed', { defaultValue: 'Runner restart failed' })}: ${r.runner?.error ?? st}`)
      } else {
        setNotice(t('msg.restarted', { defaultValue: 'Runner restarted ({{status}}).', status: st || 'ok' }))
      }
      await load()
    } catch (err) {
      setError(`${t('msg.restartFailed', { defaultValue: 'Runner restart failed' })}: ${(err as Error).message}`)
    } finally {
      setBusy(null)
    }
  }

  const runner = status?.runner
  const ws = status?.workspace
  const admin = access.isAdmin
  const lang = i18n.language

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '1.5rem' }}>
      <PageHeader
        title={t('settings.title', { defaultValue: 'AI assistant' })}
        subtitle={t('settings.subtitle', { defaultValue: 'Claude Code in a sandboxed runner container — status, sign-in and workspace.' })}
        actions={
          <>
            <Button icon="forum" label={t('settings.toChat', { defaultValue: 'Open chat' })} onClick={goChat} variant="ghost" />
            <Button icon="refresh" label={t('common.refresh', { defaultValue: 'Refresh' })} onClick={() => void load()} />
            {admin && (
              <>
                <Button icon="sync" label={t('settings.syncWorkspace', { defaultValue: 'Sync workspace' })} onClick={() => void sync()} busy={busy === 'sync'} disabled={busy !== null}
                  title={t('settings.syncHint', { defaultValue: 'Pull the workspace repository and regenerate CLAUDE.md from every plugin\'s AI instructions.' })} />
                <Button icon="restart_alt" label={t('settings.restartRunner', { defaultValue: 'Restart runner' })} onClick={() => void restart()} busy={busy === 'restart'} disabled={busy !== null}
                  title={t('settings.restartHint', { defaultValue: 'Recreate the runner container with the current settings (credential, model, image).' })} />
                <Button icon="terminal" label={t('settings.runnerLogs', { defaultValue: 'Runner logs' })} onClick={() => setShowLogs(true)} />
              </>
            )}
          </>
        }
      />

      {error && <ErrorBox msg={error} onDismiss={() => setError(null)} />}
      {notice && <NoticeBox msg={notice} />}
      {status === null && !error && <Spinner label={t('common.loading', { defaultValue: 'Loading…' })} />}

      {status && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(290px, 1fr))', gap: '1rem', marginBottom: '1rem' }}>
          <Card title={t('status.runnerTitle', { defaultValue: 'Runner' })} icon="dns"
            actions={<Badge tone={runnerTone(status)} label={String(runner?.status ?? '—')} pulse={String(runner?.status) === 'starting'} />}>
            <KV label={t('status.claudeVersion', { defaultValue: 'Claude Code' })} mono>{runner?.claude_version ? String(runner.claude_version) : '—'}</KV>
            <KV label={t('status.runnerAuth', { defaultValue: 'Login (runner)' })}>{authSummary(runner?.auth)}</KV>
            <KV label={t('status.liveSessions', { defaultValue: 'Live sessions' })}>{runner?.sessions ?? '—'}</KV>
            <KV label={t('status.runnerHost', { defaultValue: 'This instance drives the container' })}>{status.runner_host ? t('common.yes', { defaultValue: 'yes' }) : t('common.no', { defaultValue: 'no' })}</KV>
            {Array.isArray(runner?.warnings) && runner.warnings.length > 0 && (
              <div style={{ fontSize: '0.74rem', color: 'var(--lx-state-paused)', wordBreak: 'break-word', marginTop: 4 }}>
                <Icon name="warning_amber" size={14} style={{ verticalAlign: -2, marginRight: 4 }} />{(runner.warnings as unknown[]).map(String).join(' · ')}
              </div>
            )}
            {(status.runner_error || runner?.error) && (
              <div style={{ fontSize: '0.74rem', color: 'var(--lx-state-down)', wordBreak: 'break-word', marginTop: 4 }}>
                <Icon name="error_outline" size={14} style={{ verticalAlign: -2, marginRight: 4 }} />{status.runner_error || String(runner?.error)}
              </div>
            )}
          </Card>

          <Card title={t('status.workspaceTitle', { defaultValue: 'Workspace' })} icon="folder_special"
            actions={<Badge tone={ws?.initialized ? (ws.last_error ? 'paused' : 'up') : 'down'} label={ws?.initialized ? t('status.initialized', { defaultValue: 'ready' }) : t('status.notInitialized', { defaultValue: 'not initialised' })} />}>
            <KV label={t('status.path', { defaultValue: 'Path' })} mono>{ws?.path ?? '—'}</KV>
            <KV label={t('status.remote', { defaultValue: 'Remote' })} mono>{ws?.remote || t('status.localOnly', { defaultValue: 'local only' })}</KV>
            <KV label={t('status.branch', { defaultValue: 'Branch' })} mono>{ws?.branch ?? '—'}</KV>
            <KV label={t('status.dirtyFiles', { defaultValue: 'Uncommitted files' })}>{ws?.dirty_files ?? 0}</KV>
            <KV label={t('status.instructions', { defaultValue: 'CLAUDE.md' })}>{ws?.has_instructions ? t('status.present', { defaultValue: 'generated' }) : t('status.missing', { defaultValue: 'missing' })}</KV>
            <KV label={t('status.lastSync', { defaultValue: 'Last sync' })}>{fmtDateTime(ws?.last_sync ?? null, lang)}</KV>
            {ws?.last_error && (
              <div style={{ fontSize: '0.74rem', color: 'var(--lx-state-down)', wordBreak: 'break-word', marginTop: 4 }}>
                <Icon name="error_outline" size={14} style={{ verticalAlign: -2, marginRight: 4 }} />{ws.last_error}
              </div>
            )}
          </Card>

          <Card title={t('status.configTitle', { defaultValue: 'Configuration' })} icon="tune"
            actions={<Badge tone={status.auth_mode === 'none' ? 'down' : 'up'} label={status.auth_mode === 'oauth' ? 'OAuth' : status.auth_mode === 'api_key' ? 'API key' : t('login.modeNone', { defaultValue: 'not signed in' })} />}>
            <KV label={t('status.provider', { defaultValue: 'Provider' })} mono>{status.provider}</KV>
            <KV label={t('status.model', { defaultValue: 'Model' })} mono>{status.model || t('status.modelDefault', { defaultValue: 'default' })}</KV>
            <KV label={t('status.permissionMode', { defaultValue: 'Permission mode' })} mono>{status.permission_mode}</KV>
            <KV label={t('status.dbReady', { defaultValue: 'Transcript storage' })}>{status.db_ready ? t('common.ready', { defaultValue: 'ready' }) : t('common.notReady', { defaultValue: 'not ready' })}</KV>
            <p style={{ margin: '0.5rem 0 0', fontSize: '0.74rem', color: 'var(--lx-text-muted)' }}>
              <Icon name="settings" size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
              {t('settings.coreSettingsHint', { defaultValue: 'Credential, model, permission mode, workspace repository, allowed Vault spaces and other guardrails are edited in the plugin settings dialog (gear icon on the Plugins page).' })}
            </p>
          </Card>
        </div>
      )}

      {status && !admin && access.loaded && (
        <InfoBox msg={t('settings.readOnly', { defaultValue: 'Sign-in, workspace sync and runner control require the plugin admin permission.' })} icon="lock" />
      )}

      {admin && (
        <div style={{ marginBottom: '1rem' }}>
          <ClaudeLogin status={status} onChanged={() => void load()} />
        </div>
      )}

      <Card title={t('instructions.title', { defaultValue: 'Generated instructions (CLAUDE.md)' })} icon="menu_book"
        actions={
          <>
            {instructionsOpen && <Button icon="refresh" label={t('common.refresh', { defaultValue: 'Refresh' })} variant="ghost" onClick={() => void loadInstructions()} />}
            <Button icon={instructionsOpen ? 'expand_less' : 'expand_more'} label={instructionsOpen ? t('common.hide', { defaultValue: 'Hide' }) : t('common.show', { defaultValue: 'Show' })} variant="ghost" onClick={() => setInstructionsOpen((o) => !o)} />
          </>
        }>
        <p style={{ margin: 0, fontSize: '0.78rem', color: 'var(--lx-text-muted)' }}>
          {t('instructions.body', { defaultValue: 'Read-only preview of the CLAUDE.md the plugin writes into the workspace: the Lyndrix base instructions, every loaded plugin\'s ai_instructions and the operator\'s extra instructions. Regenerated on every workspace sync.' })}
        </p>
        {instructionsOpen && (
          <>
            {instructionsError && <ErrorBox msg={instructionsError} />}
            {instructions === null && !instructionsError && <Spinner label={t('common.loading', { defaultValue: 'Loading…' })} compact />}
            {instructions !== null && (
              <pre className="lx-log-panel lxai-pre lxai-scroll" style={{ maxHeight: 520 }}>
                {instructions || t('instructions.empty', { defaultValue: '(empty — the workspace has not been synchronised yet)' })}
              </pre>
            )}
          </>
        )}
      </Card>

      {showLogs && <RunnerLogsModal onClose={() => setShowLogs(false)} />}
    </div>
  )
}
