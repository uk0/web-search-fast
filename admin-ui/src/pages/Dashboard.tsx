import { useEffect, useState } from 'react'
import {
  api, type Stats, type SearchLog, type SystemInfo, type Analytics, type DbHealth, type TabsInfo,
  type PerfStats, type PerfSectionError, type EngineHealth,
} from '@/lib/api'
import {
  Search, Key, ShieldBan, Activity, Monitor, Globe,
  TrendingUp, Zap, BarChart3, Database, Cpu, CheckCircle2, XCircle, Layers,
  HardDrive, CircuitBoard, Timer, Trash2,
} from 'lucide-react'
import {
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, BarChart, Bar, Area, AreaChart,
} from 'recharts'

function formatHour(hour: string): string {
  return hour.split(' ')[1] || hour
}

/* ── Circular gauge for system metrics ── */
function Gauge({ value, color, size = 56 }: { value: number; color: string; size?: number }) {
  const r = (size - 8) / 2
  const circ = 2 * Math.PI * r
  const offset = circ - (value / 100) * circ
  return (
    <svg width={size} height={size} className="transform -rotate-90">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none"
        stroke="rgba(0,0,0,0.06)" strokeWidth={5} />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none"
        stroke={color} strokeWidth={5} strokeLinecap="round"
        strokeDasharray={circ} strokeDashoffset={offset}
        style={{ transition: 'stroke-dashoffset 0.6s ease' }} />
    </svg>
  )
}

/* ── /perf section helpers ── */
function isPerfError(section: unknown): section is PerfSectionError {
  return (
    typeof section === 'object' && section !== null &&
    typeof (section as PerfSectionError).error === 'string'
  )
}

function breakerBadge(state?: string): { background: string; color: string } {
  switch (state) {
    case 'closed': return { background: 'rgba(52,199,89,0.1)', color: 'var(--accent-green)' }
    case 'half_open': return { background: 'rgba(255,149,0,0.1)', color: 'var(--accent-orange)' }
    case 'open': return { background: 'rgba(255,59,48,0.1)', color: 'var(--accent-red)' }
    default: return { background: 'rgba(0,0,0,0.05)', color: 'var(--text-tertiary)' }
  }
}

function fmtCount(v: number | null | undefined): string {
  return v != null ? v.toLocaleString() : '—'
}

/* ── Custom chart tooltip ── */
function GlassTooltip({ active, payload, label, suffix = '' }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="glass-heavy px-3 py-2 text-xs" style={{ borderRadius: '10px' }}>
      <p style={{ color: 'var(--text-tertiary)' }}>{label}</p>
      {payload.map((p: any) => (
        <p key={p.name} style={{ color: p.color }} className="font-medium">
          {p.name}: {p.value}{suffix}
        </p>
      ))}
    </div>
  )
}

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [logs, setLogs] = useState<SearchLog[]>([])
  const [systemInfo, setSystemInfo] = useState<SystemInfo | null>(null)
  const [analytics, setAnalytics] = useState<Analytics | null>(null)
  const [dbHealth, setDbHealth] = useState<DbHealth | null>(null)
  const [tabs, setTabs] = useState<TabsInfo | null>(null)
  const [perf, setPerf] = useState<PerfStats | null>(null)
  const [clearingCache, setClearingCache] = useState(false)
  const [timeRange, setTimeRange] = useState<'24h' | '7d'>('24h')
  const [error, setError] = useState('')

  useEffect(() => {
    api.getStats().then(setStats).catch((e) => setError(e.message))
    api.getSearchLogs({ page_size: '5' }).then((r) => setLogs(r.items)).catch(() => {})
    api.getSystem().then(setSystemInfo).catch(() => {})
    api.getAnalytics(24).then(setAnalytics).catch(() => {})
    api.getDbHealth().then(setDbHealth).catch(() => {})
    api.getTabs().then(setTabs).catch(() => {})
    api.getPerf().then(setPerf).catch(() => {})

    const sysInterval = setInterval(() => {
      api.getSystem().then(setSystemInfo).catch(() => {})
      api.getDbHealth().then(setDbHealth).catch(() => {})
      api.getTabs().then(setTabs).catch(() => {})
      api.getPerf().then(setPerf).catch(() => {})
    }, 10000)
    const analyticsInterval = setInterval(() => {
      const hours = timeRange === '7d' ? 168 : 24
      api.getAnalytics(hours).then(setAnalytics).catch(() => {})
    }, 30000)
    return () => { clearInterval(sysInterval); clearInterval(analyticsInterval) }
  }, [])

  useEffect(() => {
    const hours = timeRange === '7d' ? 168 : 24
    api.getAnalytics(hours).then(setAnalytics).catch(() => {})
  }, [timeRange])

  if (error) return <div className="glass p-6" style={{ color: 'var(--accent-red)' }}>Error: {error}</div>
  if (!stats) return (
    <div className="flex items-center justify-center h-64" style={{ color: 'var(--text-tertiary)' }}>
      Loading...
    </div>
  )

  const statCards = [
    { label: 'Total Searches', value: stats.total_searches, icon: Search, color: 'var(--accent-blue)', bg: 'rgba(0, 122, 255, 0.08)' },
    { label: 'Today', value: stats.searches_today, icon: Activity, color: 'var(--accent-green)', bg: 'rgba(52, 199, 89, 0.08)' },
    { label: 'Active Keys', value: stats.active_keys, icon: Key, color: 'var(--accent-purple)', bg: 'rgba(175, 82, 222, 0.08)' },
    { label: 'Banned IPs', value: stats.banned_ips, icon: ShieldBan, color: 'var(--accent-red)', bg: 'rgba(255, 59, 48, 0.08)' },
  ]

  const cpuPct = systemInfo?.cpu_percent ?? 0
  const memPct = systemInfo?.memory.percent ?? 0
  const cpuColor = cpuPct < 50 ? 'var(--accent-green)' : cpuPct < 80 ? 'var(--accent-orange)' : 'var(--accent-red)'
  const memColor = memPct < 50 ? 'var(--accent-green)' : memPct < 80 ? 'var(--accent-orange)' : 'var(--accent-red)'

  const successRate = analytics?.success_rate ?? null
  const successColor = successRate === null ? 'var(--text-tertiary)'
    : successRate >= 95 ? 'var(--accent-green)'
    : successRate >= 80 ? 'var(--accent-orange)' : 'var(--accent-red)'

  // /perf sections: each is stats, {error} (collector threw) or undefined (not loaded)
  const rawCache = perf?.cache
  const cache = rawCache && !isPerfError(rawCache) ? rawCache : null
  const cacheError = rawCache && isPerfError(rawCache) ? rawCache.error : null
  const rawEngines = perf?.engines
  const engines = rawEngines && !isPerfError(rawEngines) ? rawEngines : null
  const enginesError = rawEngines && isPerfError(rawEngines) ? rawEngines.error : null
  const rawRateLimit = perf?.rate_limit
  const rateLimit = rawRateLimit && !isPerfError(rawRateLimit) ? rawRateLimit : null
  const rateLimitError = rawRateLimit && isPerfError(rawRateLimit) ? rawRateLimit.error : null

  const engineRows: [string, EngineHealth][] = engines ? Object.entries(engines) : []
  const openEngines = engineRows.filter(([, e]) => e?.state === 'open').length
  const hitRatePct = (cache?.hit_rate ?? 0) * 100
  const hitRateColor = hitRatePct >= 60 ? 'var(--accent-green)'
    : hitRatePct >= 25 ? 'var(--accent-orange)' : 'var(--accent-red)'
  const throttledTotal = rateLimit?.throttled_total ?? 0

  const clearCache = async () => {
    setClearingCache(true)
    try {
      await api.clearCache()
      setPerf(await api.getPerf())
    } catch {
      // keep the last snapshot; next poll refreshes anyway
    } finally {
      setClearingCache(false)
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <BarChart3 className="h-6 w-6" style={{ color: 'var(--accent-blue)' }} />
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)' }}>Dashboard</h1>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map((c) => (
          <div key={c.label} className="glass p-5">
            <div className="flex items-center justify-between">
              <span className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>{c.label}</span>
              <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: c.bg }}>
                <c.icon className="h-[18px] w-[18px]" style={{ color: c.color }} />
              </div>
            </div>
            <p className="mt-3 text-3xl font-bold tracking-tight" style={{ color: 'var(--text-primary)' }}>
              {c.value.toLocaleString()}
            </p>
          </div>
        ))}
      </div>

      {/* System monitoring */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* CPU */}
        <div className="glass p-5">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>CPU</p>
              <p className="text-2xl font-bold mt-1" style={{ color: cpuColor }}>
                {systemInfo ? `${cpuPct.toFixed(1)}%` : '—'}
              </p>
            </div>
            {systemInfo && <Gauge value={cpuPct} color={cpuColor} />}
          </div>
        </div>

        {/* Memory */}
        <div className="glass p-5">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Memory</p>
              <p className="text-2xl font-bold mt-1" style={{ color: memColor }}>
                {systemInfo ? `${memPct.toFixed(1)}%` : '—'}
              </p>
              {systemInfo && (
                <p className="text-xs mt-0.5" style={{ color: 'var(--text-tertiary)' }}>
                  {systemInfo.memory.used_gb.toFixed(1)} / {systemInfo.memory.total_gb.toFixed(1)} GB
                </p>
              )}
            </div>
            {systemInfo && <Gauge value={memPct} color={memColor} />}
          </div>
        </div>

        {/* Process */}
        <div className="glass p-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center"
                 style={{ background: 'rgba(255, 149, 0, 0.08)' }}>
              <Monitor className="h-[18px] w-[18px]" style={{ color: 'var(--accent-orange)' }} />
            </div>
            <div>
              <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Process RSS</p>
              <p className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
                {systemInfo ? `${systemInfo.process.rss_mb.toFixed(0)} MB` : '—'}
              </p>
            </div>
          </div>
        </div>

        {/* Browser Pool */}
        <div className="glass p-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center"
                 style={{ background: 'rgba(90, 200, 250, 0.08)' }}>
              <Globe className="h-[18px] w-[18px]" style={{ color: 'var(--accent-teal)' }} />
            </div>
            <div>
              <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Browser Pool</p>
              {systemInfo ? (
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-2 w-2 rounded-full ${systemInfo.pool.started ? 'bg-green-500' : 'bg-red-500'}`} />
                  <p className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
                    {systemInfo.pool.active_tabs} / {systemInfo.pool.pool_size}
                  </p>
                </div>
              ) : (
                <p className="text-2xl font-bold" style={{ color: 'var(--text-tertiary)' }}>—</p>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Browser instance + Database health */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Browser instance detail */}
        <div className="glass p-5">
          <div className="flex items-center gap-2 mb-4">
            <Cpu className="h-5 w-5" style={{ color: 'var(--accent-teal)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Browser Instance</h2>
            {systemInfo && (
              <span className="glass-badge ml-auto" style={{
                background: systemInfo.pool.started ? 'rgba(52,199,89,0.1)' : 'rgba(255,59,48,0.1)',
                color: systemInfo.pool.started ? 'var(--accent-green)' : 'var(--accent-red)',
              }}>{systemInfo.pool.started ? 'running' : 'down'}</span>
            )}
          </div>
          {systemInfo ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2.5 text-sm">
              {[
                ['Pool (active / size / max)', `${systemInfo.pool.active_tabs} / ${systemInfo.pool.pool_size} / ${systemInfo.pool.max_pool_size}`],
                ['Total requests', systemInfo.pool.total_requests.toLocaleString()],
                ['Total failures', systemInfo.pool.total_failures.toLocaleString()],
                ['Restarts / Recycles', `${systemInfo.pool.restart_count} / ${systemInfo.pool.recycle_count ?? 0}`],
                ['Generation', String(systemInfo.pool.generation ?? '—')],
                ['Proxies', String(systemInfo.pool.proxy_count ?? 0)],
              ].map(([k, v]) => (
                <div key={k} className="flex items-center justify-between border-b py-1" style={{ borderColor: 'rgba(0,0,0,0.04)' }}>
                  <span style={{ color: 'var(--text-tertiary)' }}>{k}</span>
                  <span className="font-mono font-medium" style={{ color: 'var(--text-primary)' }}>{v}</span>
                </div>
              ))}
              <div className="col-span-2 flex flex-wrap gap-1.5 mt-1">
                {[
                  ['headless', systemInfo.pool.headless],
                  ['geo-fingerprint', systemInfo.pool.geo_fingerprint],
                  ['block-resources', systemInfo.pool.block_resources],
                  ['block-webrtc', systemInfo.pool.block_webrtc],
                ].map(([label, on]) => (
                  <span key={String(label)} className="glass-badge text-[11px]" style={{
                    background: on ? 'rgba(52,199,89,0.1)' : 'rgba(0,0,0,0.05)',
                    color: on ? 'var(--accent-green)' : 'var(--text-tertiary)',
                  }}>{on ? '✓' : '○'} {label}</span>
                ))}
              </div>
            </div>
          ) : <p className="text-sm" style={{ color: 'var(--text-tertiary)' }}>Loading…</p>}
        </div>

        {/* Database health */}
        <div className="glass p-5">
          <div className="flex items-center gap-2 mb-4">
            <Database className="h-5 w-5" style={{ color: 'var(--accent-blue)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Database</h2>
            {dbHealth && (
              <span className="glass-badge ml-auto inline-flex items-center gap-1" style={{
                background: dbHealth.ok ? 'rgba(52,199,89,0.1)' : 'rgba(255,59,48,0.1)',
                color: dbHealth.ok ? 'var(--accent-green)' : 'var(--accent-red)',
              }}>
                {dbHealth.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                {dbHealth.ok ? 'healthy' : 'issue'}
              </span>
            )}
          </div>
          {dbHealth ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2.5 text-sm">
              {[
                ['Integrity', dbHealth.integrity],
                ['Journal mode', dbHealth.journal_mode],
                ['Size', `${(dbHealth.size_bytes / 1024 / 1024).toFixed(2)} MB`],
                ['Search logs', String(dbHealth.tables.search_logs ?? '—')],
                ['API keys', String(dbHealth.tables.api_keys ?? '—')],
                ['Proxies / IP bans', `${dbHealth.tables.proxies ?? '—'} / ${dbHealth.tables.ip_bans ?? '—'}`],
              ].map(([k, v]) => (
                <div key={k} className="flex items-center justify-between border-b py-1" style={{ borderColor: 'rgba(0,0,0,0.04)' }}>
                  <span style={{ color: 'var(--text-tertiary)' }}>{k}</span>
                  <span className="font-mono font-medium" style={{ color: 'var(--text-primary)' }}>{v}</span>
                </div>
              ))}
            </div>
          ) : <p className="text-sm" style={{ color: 'var(--text-tertiary)' }}>Loading…</p>}
        </div>
      </div>

      {/* Search performance — cache / engine circuit breakers / rate limiting */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Search cache */}
        <div className="glass p-5">
          <div className="flex items-center gap-2 mb-4">
            <HardDrive className="h-5 w-5" style={{ color: 'var(--accent-green)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Search Cache</h2>
            {cache && cache.enabled === false && (
              <span className="glass-badge" style={{ background: 'rgba(0,0,0,0.05)', color: 'var(--text-tertiary)' }}>
                disabled
              </span>
            )}
            {cache && (
              <button onClick={clearCache} disabled={clearingCache}
                className="glass-btn glass-btn-ghost ml-auto px-2.5 py-1 text-xs flex items-center gap-1"
                style={{ color: 'var(--accent-red)', opacity: clearingCache ? 0.5 : 1 }}>
                <Trash2 className="h-3 w-3" />
                {clearingCache ? 'Clearing…' : 'Clear'}
              </button>
            )}
          </div>
          {cache ? (
            <>
              <div className="flex items-end gap-2 mb-3">
                <p className="text-4xl font-bold tracking-tight" style={{ color: hitRateColor }}>
                  {hitRatePct.toFixed(1)}%
                </p>
                <p className="text-sm font-medium pb-1" style={{ color: 'var(--text-secondary)' }}>hit rate</p>
              </div>
              <div className="grid grid-cols-1 gap-y-2.5 text-sm">
                {[
                  ['Hits / Misses', `${fmtCount(cache.hits)} / ${fmtCount(cache.misses)}`],
                  ['Entries', `${fmtCount(cache.size)} / ${fmtCount(cache.max_size)}`],
                  ['TTL', cache.ttl != null ? `${cache.ttl}s` : '—'],
                  ['Backend', cache.backend ?? '—'],
                  ...(cache.inflight != null ? [['In-flight', String(cache.inflight)]] : []),
                ].map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between border-b py-1" style={{ borderColor: 'rgba(0,0,0,0.04)' }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{k}</span>
                    <span className="font-mono font-medium" style={{ color: 'var(--text-primary)' }}>{v}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm py-4 text-center" style={{ color: 'var(--text-tertiary)' }}>
              {cacheError ? `Stats unavailable: ${cacheError}` : 'Loading…'}
            </p>
          )}
        </div>

        {/* Engine circuit breakers */}
        <div className="glass p-5 lg:col-span-2">
          <div className="flex items-center gap-2 mb-4">
            <CircuitBoard className="h-5 w-5" style={{ color: 'var(--accent-purple)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Engine Health</h2>
            {openEngines > 0 && (
              <span className="glass-badge ml-auto" style={{ background: 'rgba(255,59,48,0.1)', color: 'var(--accent-red)' }}>
                {openEngines} circuit-broken
              </span>
            )}
          </div>
          {engineRows.length > 0 ? (
            <table className="glass-table">
              <thead>
                <tr><th>Engine</th><th>State</th><th>Fails</th><th>EWMA</th><th>Success</th><th>Cooldown</th></tr>
              </thead>
              <tbody>
                {engineRows.map(([name, e]) => (
                  <tr key={name}>
                    <td className="font-medium">{name}</td>
                    <td>
                      <span className="glass-badge" style={breakerBadge(e?.state)}>{e?.state ?? 'unknown'}</span>
                    </td>
                    <td className="font-mono text-xs"
                        style={{ color: (e?.consecutive_failures ?? 0) > 0 ? 'var(--accent-orange)' : 'var(--text-secondary)' }}>
                      {e?.consecutive_failures ?? 0}
                    </td>
                    <td style={{ color: 'var(--text-secondary)' }}>
                      {e?.ewma_latency_ms != null ? `${Math.round(e.ewma_latency_ms)}ms` : '—'}
                    </td>
                    <td style={{ color: 'var(--text-secondary)' }}>
                      {e?.success_rate != null ? `${(e.success_rate * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td style={{ color: e?.state === 'open' ? 'var(--accent-red)' : 'var(--text-tertiary)' }}>
                      {e?.state === 'open' && e?.cooldown_remaining_s != null ? `${Math.ceil(e.cooldown_remaining_s)}s` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm py-4 text-center" style={{ color: 'var(--text-tertiary)' }}>
              {enginesError ? `Stats unavailable: ${enginesError}` : perf ? 'No engine data yet' : 'Loading…'}
            </p>
          )}
        </div>

        {/* Rate limiting */}
        <div className="glass p-5 lg:col-span-3">
          <div className="flex items-center gap-2 mb-4">
            <Timer className="h-5 w-5" style={{ color: 'var(--accent-orange)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Rate Limiting</h2>
            {rateLimit && (
              <span className="glass-badge ml-auto" style={{
                background: rateLimit.enabled ? 'rgba(52,199,89,0.1)' : 'rgba(0,0,0,0.05)',
                color: rateLimit.enabled ? 'var(--accent-green)' : 'var(--text-tertiary)',
              }}>{rateLimit.enabled ? 'enabled' : 'disabled'}</span>
            )}
          </div>
          {rateLimit ? (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <div>
                <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Throttled</p>
                <p className="text-2xl font-bold mt-1"
                   style={{ color: throttledTotal > 0 ? 'var(--accent-orange)' : 'var(--text-primary)' }}>
                  {throttledTotal.toLocaleString()}
                </p>
              </div>
              <div>
                <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Active Buckets</p>
                <p className="text-2xl font-bold mt-1" style={{ color: 'var(--text-primary)' }}>
                  {fmtCount(rateLimit.active_buckets)}
                  <span className="text-sm font-medium ml-1" style={{ color: 'var(--text-tertiary)' }}>
                    / {fmtCount(rateLimit.max_buckets)}
                  </span>
                </p>
              </div>
              <div>
                <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Rate</p>
                <p className="text-2xl font-bold mt-1" style={{ color: 'var(--text-primary)' }}>
                  {rateLimit.rps != null ? `${rateLimit.rps}/s` : '—'}
                  <span className="text-sm font-medium ml-1" style={{ color: 'var(--text-tertiary)' }}>
                    burst {fmtCount(rateLimit.burst)}
                  </span>
                </p>
              </div>
              <div>
                <p className="text-[13px] font-medium" style={{ color: 'var(--text-secondary)' }}>Concurrency</p>
                <p className="text-2xl font-bold mt-1" style={{ color: 'var(--text-primary)' }}>
                  {fmtCount(rateLimit.concurrency)}
                </p>
              </div>
            </div>
          ) : (
            <p className="text-sm py-4 text-center" style={{ color: 'var(--text-tertiary)' }}>
              {rateLimitError ? `Stats unavailable: ${rateLimitError}` : 'Loading…'}
            </p>
          )}
        </div>
      </div>

      {/* Latency chart */}
      <div className="glass p-5">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5" style={{ color: 'var(--accent-blue)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Search Latency</h2>
          </div>
          <div className="flex gap-1 p-0.5 rounded-lg" style={{ background: 'rgba(0,0,0,0.04)' }}>
            {(['24h', '7d'] as const).map((r) => (
              <button key={r} onClick={() => setTimeRange(r)}
                className="glass-btn px-3 py-1 text-xs font-medium transition-all"
                style={{
                  background: timeRange === r ? 'white' : 'transparent',
                  color: timeRange === r ? 'var(--accent-blue)' : 'var(--text-tertiary)',
                  boxShadow: timeRange === r ? '0 1px 3px rgba(0,0,0,0.08)' : 'none',
                  borderRadius: '6px',
                }}>
                {r}
              </button>
            ))}
          </div>
        </div>
        {analytics && analytics.timeline.length > 0 ? (
          <ResponsiveContainer width="100%" height={280}>
            <AreaChart data={analytics.timeline}>
              <defs>
                <linearGradient id="avgGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#007aff" stopOpacity={0.15} />
                  <stop offset="100%" stopColor="#007aff" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="p95Grad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#ff9500" stopOpacity={0.1} />
                  <stop offset="100%" stopColor="#ff9500" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
              <XAxis dataKey="hour" tickFormatter={formatHour} tick={{ fill: '#aeaeb2', fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis unit=" ms" tick={{ fill: '#aeaeb2', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<GlassTooltip suffix=" ms" />} />
              <Area type="monotone" dataKey="avg_ms" stroke="#007aff" strokeWidth={2} fill="url(#avgGrad)" name="Avg" dot={false} />
              <Area type="monotone" dataKey="p95_ms" stroke="#ff9500" strokeWidth={2} fill="url(#p95Grad)" name="P95" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-[280px] flex items-center justify-center" style={{ color: 'var(--text-tertiary)' }}>
            {analytics ? 'No latency data yet' : 'Loading...'}
          </div>
        )}
      </div>

      {/* Engine distribution + success rate */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="glass p-5 lg:col-span-2">
          <div className="flex items-center gap-2 mb-5">
            <Zap className="h-5 w-5" style={{ color: 'var(--accent-purple)' }} />
            <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Engine Distribution</h2>
          </div>
          {analytics && analytics.engines.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={analytics.engines} barSize={48}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
                <XAxis dataKey="name" tick={{ fill: '#6e6e73', fontSize: 12 }} axisLine={false} tickLine={false} />
                <YAxis allowDecimals={false} tick={{ fill: '#aeaeb2', fontSize: 11 }} axisLine={false} tickLine={false} />
                <Tooltip content={<GlassTooltip />} />
                <Bar dataKey="count" fill="#af52de" radius={[8, 8, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[240px] flex items-center justify-center text-sm" style={{ color: 'var(--text-tertiary)' }}>
              {analytics ? 'No engine data yet' : 'Loading...'}
            </div>
          )}
        </div>

        <div className="glass p-5 flex flex-col items-center justify-center">
          <p className="text-6xl font-bold tracking-tight" style={{ color: successColor }}>
            {successRate !== null ? `${successRate.toFixed(1)}%` : '—'}
          </p>
          <p className="mt-2 text-sm font-medium" style={{ color: 'var(--text-secondary)' }}>Success Rate</p>
        </div>
      </div>

      {/* Recent searches */}
      <div className="glass p-5">
        <div className="flex items-center gap-2 mb-4">
          <Search className="h-5 w-5" style={{ color: 'var(--accent-blue)' }} />
          <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Recent Searches</h2>
        </div>
        {logs.length === 0 ? (
          <p className="text-sm py-4 text-center" style={{ color: 'var(--text-tertiary)' }}>No searches yet</p>
        ) : (
          <table className="glass-table">
            <thead>
              <tr>
                <th>Query</th>
                <th>Engine</th>
                <th>IP</th>
                <th>Latency</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td className="font-medium">{l.query}</td>
                  <td>
                    <span className="glass-badge" style={{ background: 'rgba(175, 82, 222, 0.08)', color: 'var(--accent-purple)' }}>
                      {l.engine || '—'}
                    </span>
                  </td>
                  <td className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{l.ip_address}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{l.elapsed_ms ? `${l.elapsed_ms}ms` : '—'}</td>
                  <td style={{ color: 'var(--text-tertiary)' }}>{new Date(l.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Live browser tabs — instance(generation) / tab / session map */}
      <div className="glass p-5">
        <div className="flex items-center gap-2 mb-4">
          <Layers className="h-5 w-5" style={{ color: 'var(--accent-teal)' }} />
          <h2 className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>Live Browser Tabs</h2>
          {tabs && (
            <span className="glass-badge ml-auto" style={{ background: 'rgba(90,200,250,0.1)', color: 'var(--accent-teal)' }}>
              instance gen {tabs.generation} · {tabs.active} active
            </span>
          )}
        </div>
        {tabs && tabs.tabs.length > 0 ? (
          <table className="glass-table">
            <thead>
              <tr><th>Tab</th><th>Instance (gen)</th><th>Req #</th><th>Session / Label</th><th>Age</th></tr>
            </thead>
            <tbody>
              {tabs.tabs.map((t) => (
                <tr key={t.tab_id}>
                  <td className="font-mono text-xs">#{t.tab_id}</td>
                  <td className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>gen-{t.generation}</td>
                  <td className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{t.req_id ?? '—'}</td>
                  <td className="text-xs" style={{ color: 'var(--text-secondary)' }}>{t.session || t.label || '—'}</td>
                  <td className="text-xs" style={{ color: t.age_secs > 60 ? 'var(--accent-red)' : 'var(--text-tertiary)' }}>
                    {t.age_secs.toFixed(1)}s
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-sm py-4 text-center" style={{ color: 'var(--text-tertiary)' }}>No active tabs</p>
        )}
      </div>
    </div>
  )
}
