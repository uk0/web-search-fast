import { useEffect, useState, useCallback } from 'react'
import { api, type Proxy, type ProxyStats, type ProxyTestResult } from '@/lib/api'
import { Network, Trash2, ToggleLeft, ToggleRight, Upload, ChevronDown, Zap, Loader2, CheckCircle2, XCircle } from 'lucide-react'

type TestState = { loading: boolean; result?: ProxyTestResult; error?: string; ts?: number }

function latencyColor(ms: number): string {
  if (ms < 500) return 'var(--accent-green)'
  if (ms < 1500) return 'var(--accent-orange)'
  return 'var(--accent-red)'
}

function TestResultCell({ state }: { state?: TestState }) {
  if (!state) return <span className="text-xs" style={{ color: 'var(--text-tertiary)' }}>—</span>
  if (state.loading) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: 'var(--text-tertiary)' }}>
        <Loader2 className="h-3 w-3 animate-spin" />
        Testing...
      </span>
    )
  }
  if (state.error) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: 'var(--accent-red)' }} title={state.error}>
        <XCircle className="h-3.5 w-3.5" />
        {state.error.length > 24 ? state.error.slice(0, 24) + '…' : state.error}
      </span>
    )
  }
  const r = state.result!
  if (r.ok) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs font-medium" style={{ color: latencyColor(r.latency_ms) }}>
        <CheckCircle2 className="h-3.5 w-3.5" />
        {r.latency_ms} ms
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: 'var(--accent-red)' }} title={r.error || 'failed'}>
      <XCircle className="h-3.5 w-3.5" />
      {(r.error || 'failed').length > 24 ? (r.error || 'failed').slice(0, 24) + '…' : (r.error || 'failed')}
    </span>
  )
}

const SCHEME_OPTIONS = [
  { value: '', label: 'Auto Detect' },
  { value: 'socks5h', label: 'SOCKS5H' },
  { value: 'socks5', label: 'SOCKS5' },
  { value: 'http', label: 'HTTP' },
  { value: 'https', label: 'HTTPS' },
]

export default function ProxyManager() {
  const [proxies, setProxies] = useState<Proxy[]>([])
  const [stats, setStats] = useState<ProxyStats | null>(null)
  const [importText, setImportText] = useState('')
  const [scheme, setScheme] = useState('')
  const [importing, setImporting] = useState(false)
  const [message, setMessage] = useState('')
  const [tests, setTests] = useState<Record<number, TestState>>({})
  const [batchTesting, setBatchTesting] = useState(false)

  const load = useCallback(() => {
    api.getProxies().then(setProxies).catch(() => {})
    api.getProxyStats().then(setStats).catch(() => {})
  }, [])

  useEffect(() => { load() }, [load])

  const handleImport = async () => {
    const urls = importText.split('\n').map(l => l.trim()).filter(Boolean)
    if (urls.length === 0) return
    setImporting(true)
    setMessage('')
    try {
      const res = await api.importProxies(urls, scheme || undefined)
      setMessage(`Imported ${res.added} proxies`)
      setImportText('')
      load()
    } catch (e) {
      setMessage(`Error: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setImporting(false)
    }
  }

  const handleToggle = async (id: number, currentActive: boolean) => {
    await api.toggleProxy(id, !currentActive)
    load()
  }

  const handleDelete = async (id: number) => {
    await api.deleteProxy(id)
    load()
  }

  const handleTest = async (id: number) => {
    setTests((prev) => ({ ...prev, [id]: { loading: true } }))
    try {
      const result = await api.testProxy(id)
      setTests((prev) => ({ ...prev, [id]: { loading: false, result, ts: Date.now() } }))
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setTests((prev) => ({ ...prev, [id]: { loading: false, error: msg || 'request failed', ts: Date.now() } }))
    }
  }

  const handleTestAll = async () => {
    if (batchTesting || proxies.length === 0) return
    setBatchTesting(true)
    try {
      const concurrency = 5
      const queue = [...proxies]
      const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
        while (queue.length) {
          const next = queue.shift()
          if (next) await handleTest(next.id)
        }
      })
      await Promise.all(workers)
    } finally {
      setBatchTesting(false)
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <Network className="h-6 w-6" style={{ color: 'var(--accent-blue)' }} />
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)' }}>
          Proxy Manager
        </h1>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          {[
            { label: 'Total', value: stats.total, color: 'var(--text-primary)' },
            { label: 'Active', value: stats.active, color: 'var(--accent-green)' },
            { label: 'Inactive', value: stats.inactive, color: 'var(--accent-orange)' },
            { label: 'Failures', value: stats.total_failures, color: 'var(--accent-red)' },
          ].map((s) => (
            <div key={s.label} className="glass p-4 text-center">
              <div className="text-2xl font-bold" style={{ color: s.color }}>{s.value}</div>
              <div className="text-xs mt-1" style={{ color: 'var(--text-tertiary)' }}>{s.label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Import Section */}
      <div className="glass p-4 space-y-3">
        <textarea
          className="glass-input w-full px-4 py-3 text-sm font-mono"
          rows={4}
          placeholder={"socks5h://user:pass@host:port\nhttp://user:pass@host:port\nOne proxy per line..."}
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <div className="relative">
            <select
              className="glass-input appearance-none pl-3 pr-8 py-2 text-sm cursor-pointer"
              value={scheme}
              onChange={(e) => setScheme(e.target.value)}
            >
              {SCHEME_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 pointer-events-none" style={{ color: 'var(--text-tertiary)' }} />
          </div>
          <button
            className="glass-btn glass-btn-primary px-4 py-2 text-sm flex items-center gap-1.5"
            onClick={handleImport}
            disabled={importing || !importText.trim()}
          >
            <Upload className="h-4 w-4" />
            {importing ? 'Importing...' : 'Import Proxies'}
          </button>
          {message && (
            <span className="text-sm" style={{ color: message.startsWith('Error') ? 'var(--accent-red)' : 'var(--accent-green)' }}>
              {message}
            </span>
          )}
        </div>
      </div>

      {/* Proxy Table */}
      <div className="glass overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: 'rgba(0,0,0,0.06)' }}>
          <span className="text-sm font-medium" style={{ color: 'var(--text-secondary)' }}>
            {proxies.length} {proxies.length === 1 ? 'proxy' : 'proxies'}
          </span>
          <button
            className="glass-btn glass-btn-ghost px-3 py-1.5 text-xs flex items-center gap-1.5"
            onClick={handleTestAll}
            disabled={batchTesting || proxies.length === 0}
            style={{ color: 'var(--accent-blue)' }}
          >
            {batchTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
            {batchTesting ? 'Testing...' : 'Test All'}
          </button>
        </div>
        <table className="glass-table">
          <thead>
            <tr>
              <th>URL</th>
              <th style={{ width: '90px' }}>Scheme</th>
              <th style={{ width: '80px' }}>Status</th>
              <th style={{ width: '80px' }}>Failures</th>
              <th style={{ width: '150px' }}>Last Used</th>
              <th style={{ width: '180px' }}>Test Result</th>
              <th style={{ width: '150px' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {proxies.length === 0 && (
              <tr>
                <td colSpan={7} className="text-center py-8" style={{ color: 'var(--text-tertiary)' }}>
                  No proxies configured
                </td>
              </tr>
            )}
            {proxies.map((p) => (
              <tr key={p.id}>
                <td className="font-mono text-xs" style={{ maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.url}
                </td>
                <td>
                  <span className="px-2 py-0.5 rounded text-xs font-medium"
                    style={{
                      background: p.scheme.startsWith('socks') ? 'rgba(88,86,214,0.1)' : 'rgba(0,122,255,0.1)',
                      color: p.scheme.startsWith('socks') ? '#5856d6' : '#007aff',
                    }}>
                    {p.scheme}
                  </span>
                </td>
                <td>
                  <span className="px-2 py-0.5 rounded text-xs font-medium"
                    style={{
                      background: p.is_active ? 'rgba(52,199,89,0.1)' : 'rgba(255,149,0,0.1)',
                      color: p.is_active ? '#34c759' : '#ff9500',
                    }}>
                    {p.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td className="text-sm" style={{ color: p.fail_count > 0 ? 'var(--accent-red)' : 'var(--text-tertiary)' }}>
                  {p.fail_count}
                </td>
                <td className="text-xs" style={{ color: 'var(--text-tertiary)' }}>
                  {p.last_used_at ? new Date(p.last_used_at).toLocaleString() : '—'}
                </td>
                <td>
                  <TestResultCell state={tests[p.id]} />
                </td>
                <td>
                  <div className="flex items-center gap-1">
                    <button
                      className="glass-btn glass-btn-ghost px-2 py-1 text-xs flex items-center gap-1"
                      style={{ color: 'var(--accent-blue)' }}
                      onClick={() => handleTest(p.id)}
                      disabled={tests[p.id]?.loading}
                      title="Quick Test"
                    >
                      {tests[p.id]?.loading
                        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        : <Zap className="h-3.5 w-3.5" />}
                    </button>
                    <button
                      className="glass-btn glass-btn-ghost px-2 py-1 text-xs flex items-center gap-1"
                      style={{ color: p.is_active ? 'var(--accent-orange)' : 'var(--accent-green)' }}
                      onClick={() => handleToggle(p.id, p.is_active)}
                      title={p.is_active ? 'Disable' : 'Enable'}
                    >
                      {p.is_active ? <ToggleRight className="h-3.5 w-3.5" /> : <ToggleLeft className="h-3.5 w-3.5" />}
                    </button>
                    <button
                      className="glass-btn glass-btn-ghost px-2 py-1 text-xs flex items-center gap-1"
                      style={{ color: 'var(--accent-red)' }}
                      onClick={() => handleDelete(p.id)}
                      title="Delete"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
