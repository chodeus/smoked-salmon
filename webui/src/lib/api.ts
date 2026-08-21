const BASE = '/api'

let unauthorizedHandler: (() => void) | null = null

/** Registered by App to flip back to the login screen when a call 401s. */
export function onUnauthorized(cb: () => void) {
  unauthorizedHandler = cb
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    unauthorizedHandler?.()
    throw new Error('Authentication required.')
  }
  if (!res.ok) throw new Error(await errorDetail(res))
  return res.json()
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(await fetch(BASE + path))
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(res)
}

export async function apiDelete<T>(path: string): Promise<T> {
  return handle<T>(await fetch(BASE + path, { method: 'DELETE' }))
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const data = await res.json()
    if (typeof data.detail === 'string') return data.detail
    return JSON.stringify(data.detail ?? data)
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

export interface AuthState {
  required: boolean
  authenticated: boolean
}

/** Whether the server requires a token and whether this browser is authenticated. */
export async function checkAuth(): Promise<AuthState> {
  try {
    const res = await fetch(BASE + '/auth')
    if (!res.ok) return { required: false, authenticated: true }
    return await res.json()
  } catch {
    return { required: false, authenticated: true }
  }
}

/** Exchange the token for a session cookie. Returns true on success. */
export async function login(token: string): Promise<boolean> {
  const res = await fetch(BASE + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
  return res.ok
}

export function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${BASE}/ws`
}
