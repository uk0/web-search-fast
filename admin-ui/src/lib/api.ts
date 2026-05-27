const BASE = '/admin/api'

function getToken(): string {
  return localStorage.getItem('admin_token') || ''
}

export function setToken(token: string) {
  localStorage.setItem('admin_token', token)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
      ...init?.headers,
    },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `HTTP ${res.status}`)
  }
  return res.json()
}

export const api = {
  getStats: () => request<Stats>('/stats'),
  getSearchLogs: (params?: Record<string, string>) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : ''
    return request<PaginatedLogs>(`/search-logs${qs}`)
  },
  getKeys: () => request<APIKey[]>('/keys'),
  createKey: (data: { name: string; call_limit: number }) =>
    request<APIKeyCreated>('/keys', { method: 'POST', body: JSON.stringify(data) }),
  deleteKey: (id: string) => request<{ ok: boolean }>(`/keys/${id}`, { method: 'DELETE' }),
  setKeyActive: (id: string, is_active: boolean) =>
    request<{ ok: boolean; is_active: boolean }>(`/keys/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active }),
    }),
  getBans: () => request<IPBan[]>('/ip-bans'),
  banIP: (data: { ip: string; reason: string }) =>
    request<IPBan>('/ip-bans', { method: 'POST', body: JSON.stringify(data) }),
  unbanIP: (ip: string) => request<{ ok: boolean }>(`/ip-bans/${ip}`, { method: 'DELETE' }),
  getSystem: () => request<SystemInfo>('/system'),
  getAnalytics: (hours = 24) => request<Analytics>(`/analytics?hours=${hours}`),
  getProxies: () => request<Proxy[]>('/proxies'),
  importProxies: (urls: string[], scheme?: string) =>
    request<{ added: number }>('/proxies', { method: 'POST', body: JSON.stringify({ urls, scheme: scheme || '' }) }),
  deleteProxy: (id: number) => request<{ ok: boolean }>(`/proxies/${id}`, { method: 'DELETE' }),
  toggleProxy: (id: number, is_active: boolean) =>
    request<{ ok: boolean }>(`/proxies/${id}`, { method: 'PATCH', body: JSON.stringify({ is_active }) }),
  testProxy: (id: number) =>
    request<ProxyTestResult>(`/proxies/${id}/test`, { method: 'POST' }),
  getProxyStats: () => request<ProxyStats>('/proxies/stats'),
}

export interface Stats {
  total_searches: number
  searches_today: number
  active_keys: number
  banned_ips: number
}

export interface SearchLog {
  id: number
  api_key_id: string | null
  query: string
  engine: string | null
  ip_address: string
  user_agent: string | null
  status_code: number | null
  elapsed_ms: number | null
  tool_name: string | null
  request_body: string | null
  response_body: string | null
  created_at: string
}

export interface PaginatedLogs {
  items: SearchLog[]
  total: number
  page: number
  page_size: number
}

export interface APIKey {
  id: string
  name: string
  key_prefix: string
  call_limit: number
  call_count: number
  is_active: boolean
  created_at: string
  expires_at: string | null
}

export interface APIKeyCreated extends APIKey {
  key: string
}

export interface IPBan {
  id: number
  ip_address: string
  reason: string
  created_at: string
}

export interface SystemInfo {
  cpu_percent: number
  memory: { total_gb: number; used_gb: number; percent: number }
  process: { rss_mb: number; vms_mb: number }
  pool: {
    started: boolean
    pool_size: number
    max_pool_size: number
    active_tabs: number
    total_requests: number
    total_failures: number
    consecutive_failures: number
    restart_count: number
  }
}

export interface TimelinePoint {
  hour: string
  avg_ms: number
  p95_ms: number
  count: number
}

export interface EngineStats {
  name: string
  count: number
}

export interface Analytics {
  timeline: TimelinePoint[]
  engines: EngineStats[]
  success_rate: number
}

export interface Proxy {
  id: number
  url: string
  scheme: string
  is_active: boolean
  fail_count: number
  last_used_at: string | null
  created_at: string
}

export interface ProxyStats {
  total: number
  active: number
  inactive: number
  total_failures: number
}

export interface ProxyTestResult {
  ok: boolean
  latency_ms: number
  error: string | null
}
