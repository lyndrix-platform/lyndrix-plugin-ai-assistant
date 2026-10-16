/**
 * Typed client for the plugin's own REST API (app/api/router.py), mounted by
 * the core at /api/plugins/lyndrix.plugin.ai_assistant/. The UI holds no
 * business logic — every capability goes through these calls; the browser
 * never talks to the runner container directly.
 */

export const PLUGIN_ID = 'lyndrix.plugin.ai_assistant'
/** Shell-side URL-safe id ("." → "-"): /apps/<id>/ai/… plugin routes. */
export const SHELL_PLUGIN_ID = 'lyndrix-plugin-ai_assistant'
export const PERM_READ = `plugin:${PLUGIN_ID}:api:read`
export const PERM_WRITE = `plugin:${PLUGIN_ID}:api:write`
export const PERM_ADMIN = `plugin:${PLUGIN_ID}:api:admin`

const TOKEN_KEY = 'lyndrix_token'
const REQUEST_TIMEOUT_MS = 20_000

// ─── Types (mirror app/api/router.py + app/logic/service.py) ─────────────────

export type AuthMode = 'oauth' | 'api_key' | 'none'

/** Bridge /health as relayed by the plugin, or one of the plugin's own
 *  placeholder states (not_started | not_bound | unreachable | error | starting | unconfigured). */
export interface RunnerHealth {
  status: string
  claude_version?: string | null
  auth?: string | boolean | Record<string, unknown> | null
  sessions?: number | null
  warnings?: string[]
  error?: string | null
  [key: string]: unknown
}

export interface WorkspaceStatus {
  path?: string
  initialized?: boolean
  remote?: string | null
  branch?: string | null
  dirty_files?: number
  has_instructions?: boolean
  last_sync?: string | null
  last_error?: string | null
}

export interface AssistantStatus {
  provider: string
  auth_mode: AuthMode
  runner: RunnerHealth
  runner_host: boolean
  runner_error: string | null
  workspace: WorkspaceStatus
  db_ready: boolean
  permission_mode: string
  model: string | null
}

export type SessionStatus = 'idle' | 'running' | 'waiting_approval' | 'closed'

export interface SessionOut {
  id: string
  owner: string
  title: string
  provider?: string
  model?: string | null
  permission_mode?: string
  claude_session_id?: string | null
  status: SessionStatus | string
  turns: number
  total_cost_usd: number
  archived?: boolean
  created_at?: string | null
  last_activity: string | null
}

export type MessageRole = 'user' | 'assistant' | 'tool' | 'system'
export type MessageKind = 'text' | 'tool_use' | 'tool_result' | 'approval' | 'result' | 'error'

export interface MessageOut {
  id: number
  role: MessageRole | string
  kind: MessageKind | string
  content: string | null
  meta: Record<string, unknown> | null
  created_at: string | null
}

export interface SessionDetail {
  session: SessionOut
  messages: MessageOut[]
  /** Bridge-side live view (opaque; may carry a seq hint / pending approvals). */
  live: Record<string, unknown>
}

/** One frame of GET /sessions/{id}/events (SSE `data:` payload). */
export interface StreamEvent {
  seq: number
  ts?: string | number
  type: string
  data: Record<string, unknown>
}

export type ApprovalDecision = 'allow' | 'deny' | 'allow_always'

export interface AuthStart {
  auth_id: string
  url: string
  expires_in: number
}

export interface AuthSubmitOut {
  ok: boolean
  restarting?: boolean
  error?: string | null
}

export interface AuthStatus {
  pending: boolean
  auth_id?: string
  /** boolean from the bridge; the auth *mode* string when the runner is down. */
  authenticated: boolean | string
  runner?: string
}

/** Core: GET /api/me/access — server-derived effective permissions. */
export interface AccessView {
  status: string
  username: string
  groups: string[]
  is_superadmin: boolean
  permissions: string[]
  plugins: Record<string, { visible: boolean; can_read: boolean; can_write: boolean; routes: string[] }>
}

// ─── Fetch wrapper ──────────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number
  retryAfter: number | null
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** The plugin's API answers 503 while the runner is down / on another instance. */
export const isRunnerUnavailable = (err: unknown): boolean => err instanceof ApiError && err.status === 503

/** In-SPA navigation the shell's BrowserRouter picks up — never a full reload
 *  (a cold load would run before the dynamic plugin routes are registered). */
export function spaNavigate(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function authHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let msg = `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') msg = body.detail
    else if (body.detail) msg = JSON.stringify(body.detail)
  } catch { /* non-JSON error body */ }
  const ra = res.headers.get('Retry-After')
  return new ApiError(res.status, msg, ra ? Number(ra) || null : null)
}

async function apiFetch<T>(path: string, init: RequestInit = {}, timeoutMs: number = REQUEST_TIMEOUT_MS): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...authHeaders(),
    ...(init.headers as Record<string, string> | undefined),
  }

  // Bound every request so a hung backend cannot leave promises pending forever.
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(path, { ...init, headers, signal: controller.signal })
  } catch (err) {
    // A timeout surfaces as a bare DOMException ("signal is aborted without reason");
    // say what happened instead.
    if ((err as { name?: string })?.name === 'AbortError') {
      throw new ApiError(0, `Zeitüberschreitung nach ${Math.round(timeoutMs / 1000)} s – das Backend hat nicht geantwortet.`)
    }
    throw err
  } finally {
    clearTimeout(timeout)
  }

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    spaNavigate('/login')
    throw new ApiError(401, 'Unauthorized')
  }
  if (!res.ok) throw await errorFromResponse(res)
  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

const base = (subpath: string) => `/api/plugins/${PLUGIN_ID}/${subpath}`

const pluginApi = {
  get: <T>(subpath: string) => apiFetch<T>(base(subpath)),
  post: <T>(subpath: string, body?: unknown, timeoutMs?: number) =>
    apiFetch<T>(base(subpath), { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }, timeoutMs),
  del: <T>(subpath: string) => apiFetch<T>(base(subpath), { method: 'DELETE' }),
}

// ─── AI assistant surface ───────────────────────────────────────────────────

export const assistantApi = {
  /** GET /status — runner health, workspace, auth mode, configured model/permission mode. */
  status: () => pluginApi.get<AssistantStatus>('status'),
  /** GET /instructions — the generated CLAUDE.md (read-only preview). */
  instructions: () => pluginApi.get<{ markdown: string }>('instructions'),
  /** POST /workspace/sync (admin) */
  syncWorkspace: () => pluginApi.post<{ ok: boolean; error?: string } & WorkspaceStatus & Record<string, unknown>>('workspace/sync'),
  /** POST /runner/restart (admin) */
  restartRunner: () => pluginApi.post<{ runner: RunnerHealth }>('runner/restart'),
  /** GET /runner/logs?tail= (admin) */
  runnerLogs: (tail: number) => pluginApi.get<{ logs: string }>(`runner/logs?tail=${encodeURIComponent(String(tail))}`),

  // Claude login (OAuth through the CLI inside the runner; admin only)
  // The CLI needs a few seconds (sometimes >20 s) before it prints the login URL.
  authStart: () => pluginApi.post<AuthStart>('auth/start', undefined, 60_000),
  authSubmit: (auth_id: string, code: string) => pluginApi.post<AuthSubmitOut>('auth/submit', { auth_id, code }),
  authStatus: () => pluginApi.get<AuthStatus>('auth/status'),

  // Sessions
  listSessions: (opts: { all?: boolean; archived?: boolean } = {}) => {
    const q = new URLSearchParams()
    if (opts.all) q.set('all', 'true')
    if (opts.archived) q.set('archived', 'true')
    const qs = q.toString()
    return pluginApi.get<{ sessions: SessionOut[] }>(`sessions${qs ? `?${qs}` : ''}`)
  },
  createSession: (data: { title?: string; permission_mode?: string } = {}) => pluginApi.post<SessionOut>('sessions', data),
  getSession: (id: string) => pluginApi.get<SessionDetail>(`sessions/${encodeURIComponent(id)}`),
  sendMessage: (id: string, content: string) =>
    pluginApi.post<Record<string, unknown>>(`sessions/${encodeURIComponent(id)}/messages`, { content }),
  approve: (id: string, approvalId: string, decision: ApprovalDecision) =>
    pluginApi.post<Record<string, unknown>>(`sessions/${encodeURIComponent(id)}/approvals/${encodeURIComponent(approvalId)}`, { decision }),
  interrupt: (id: string) => pluginApi.post<Record<string, unknown>>(`sessions/${encodeURIComponent(id)}/interrupt`),
  closeSession: (id: string) => pluginApi.del<{ ok: boolean }>(`sessions/${encodeURIComponent(id)}`),
  /** URL of the SSE proxy — consumed by lib/sse.ts with the bearer header. */
  eventsUrl: (id: string, after: number) => base(`sessions/${encodeURIComponent(id)}/events?after=${Math.max(0, Math.floor(after))}`),

  /** Core access view — used only to hide controls the API would reject anyway. */
  access: () => apiFetch<AccessView>('/api/me/access'),
}
