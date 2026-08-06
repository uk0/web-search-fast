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
  deleteKey: (id: string, hard = false) =>
    request<{ ok: boolean; deleted?: boolean }>(`/keys/${id}${hard ? '?hard=true' : ''}`, { method: 'DELETE' }),
  setKeyActive: (id: string, is_active: boolean) =>
    request<{ ok: boolean; is_active: boolean }>(`/keys/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active }),
    }),
  getDbHealth: () => request<DbHealth>('/db-health'),
  getTabs: () => request<TabsInfo>('/tabs'),
  getPerf: () => request<PerfStats>('/perf'),
  clearCache: () => request<{ ok: boolean; cleared: number }>('/cache', { method: 'DELETE' }),
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
  api_key_name: string | null
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
    recycle_count?: number
    generation?: number
    proxy_count?: number
    block_resources?: boolean
    block_webrtc?: boolean
    geo_fingerprint?: boolean
    headless?: boolean
  }
}

export interface DbHealth {
  ok: boolean
  integrity: string
  journal_mode: string
  size_bytes: number
  tables: Record<string, number>
}

export interface TabInfo {
  tab_id: number
  generation: number
  req_id: number | null
  session: string | null
  label: string | null
  age_secs: number
}

export interface TabsInfo {
  tabs: TabInfo[]
  active: number
  generation: number
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

/* --- /perf: search cache + engine circuit breakers + rate limiting --- */

/** Each /perf section degrades to `{ error }` when its stats collector throws. */
export interface PerfSectionError {
  error: string
}

// All fields optional: the panel must survive older/newer backends omitting keys.
export interface CachePerf {
  enabled?: boolean
  backend?: string // "memory" | "memory+redis"
  hits?: number
  memory_hits?: number
  redis_hits?: number
  misses?: number
  hit_rate?: number // 0..1
  size?: number
  max_size?: number
  ttl?: number // seconds
  sets?: number
  evictions?: number
  expired?: number
  errors?: number
  inflight?: number // only on backends with request coalescing
  coalesced?: number // callers served off an in-flight compute rather than a fresh one
}

export type BreakerState = 'closed' | 'open' | 'half_open'

export interface EngineHealth {
  state?: BreakerState
  consecutive_failures?: number
  failure_score?: number
  open_streak?: number
  cooldown_s?: number
  cooldown_remaining_s?: number // 0 unless state == "open"
  probe_in_flight?: boolean
  last_outcome?: 'success' | 'error' | 'blocked' | null
  last_outcome_at?: number | null // monotonic clock — only useful relative
  last_outcome_age_s?: number | null
  ewma_latency_ms?: number | null
  success_rate?: number | null // 0..1 over the sample window
  samples?: number
  totals?: { success?: number; error?: number; blocked?: number }
}

export interface RateLimitBucket {
  key?: string
  tokens?: number
  inflight?: number
  allowed?: number
  throttled?: number
  idle_s?: number
  day_count?: number
  month_count?: number
}

export interface RateLimitPerf {
  enabled?: boolean
  rps?: number
  burst?: number
  concurrency?: number
  daily_quota?: number // 0 = unlimited
  monthly_quota?: number // 0 = unlimited
  idle_ttl?: number
  active_buckets?: number
  max_buckets?: number
  throttled_total?: number
  reaped_total?: number
  keys?: RateLimitBucket[] // hottest buckets, capped server-side
}

export interface PerfStats {
  cache?: CachePerf | PerfSectionError
  engines?: Record<string, EngineHealth> | PerfSectionError
  rate_limit?: RateLimitPerf | PerfSectionError
}
