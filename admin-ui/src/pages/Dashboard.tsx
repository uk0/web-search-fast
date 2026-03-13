import { useEffect, useState } from 'react'
import { api, type Stats, type SearchLog, type SystemInfo, type Analytics } from '@/lib/api'
import {
  Search, Key, ShieldBan, Activity, Monitor, Globe,
  TrendingUp, Zap, BarChart3,
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
  const [timeRange, setTimeRange] = useState<'24h' | '7d'>('24h')
  const [error, setError] = useState('')

  useEffect(() => {
    api.getStats().then(setStats).catch((e) => setError(e.message))
    api.getSearchLogs({ page_size: '5' }).then((r) => setLogs(r.items)).catch(() => {})
    api.getSystem().then(setSystemInfo).catch(() => {})
    api.getAnalytics(24).then(setAnalytics).catch(() => {})

    const sysInterval = setInterval(() => {
      api.getSystem().then(setSystemInfo).catch(() => {})
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
    </div>
  )
}
