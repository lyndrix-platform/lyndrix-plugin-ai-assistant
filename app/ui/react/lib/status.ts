import type { AssistantStatus } from './api'

/** Plugin-side placeholder states of GET /status.runner.status that mean
 *  "no bridge to talk to" (anything else comes from the bridge's /health). */
export const RUNNER_DOWN_STATES = new Set(['not_started', 'not_bound', 'unreachable', 'error', 'starting', 'unconfigured', 'down', 'stopped'])

export function runnerIsUp(status: AssistantStatus | null): boolean {
  if (!status) return true // unknown → do not block the UI on a hunch
  return !RUNNER_DOWN_STATES.has(String(status.runner?.status ?? ''))
}
