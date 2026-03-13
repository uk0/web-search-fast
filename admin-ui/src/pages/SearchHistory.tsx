import { Fragment, useEffect, useState, useCallback } from 'react'
import { api, type SearchLog } from '@/lib/api'
import { Search, ChevronDown, ChevronRight, Clock, Cpu, User, FileText, Filter } from 'lucide-react'

function StatusBadge({ code }: { code: number | null }) {
  if (code === null) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>
  const color = code < 400 ? 'var(--accent-green)' : code < 500 ? 'var(--accent-orange)' : 'var(--accent-red)'
  const bg = code < 400 ? 'rgba(52,199,89,0.1)' : code < 500 ? 'rgba(255,149,0,0.1)' : 'rgba(255,59,48,0.1)'
  return <span className="glass-badge" style={{ background: bg, color }}>{code}</span>
}

function LatencyBadge({ ms }: { ms: number | null }) {
  if (ms === null) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>
  const color = ms < 3000 ? 'var(--accent-green)' : ms < 8000 ? 'var(--accent-orange)' : 'var(--accent-red)'
  return <span style={{ color }} className="font-mono text-xs">{ms.toLocaleString()}ms</span>
}

function DetailPanel({ log }: { log: SearchLog }) {
  return (
    <tr>
      <td colSpan={7} className="!p-0">
        <div className="animate-slide-down px-6 py-4 mx-4 mb-3 rounded-xl"
             style={{ background: 'rgba(0,0,0,0.02)' }}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Left column: metadata */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <User className="h-3.5 w-3.5" style={{ color: 'var(--text-tertiary)' }} />
                <span className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>User Agent</span>
              </div>
              <p className="text-xs font-mono pl-5.5 break-all" style={{ color: 'var(--text-secondary)' }}>
                {log.user_agent || '—'}
              </p>

              <div className="flex items-center gap-2 mt-3">
                <Cpu className="h-3.5 w-3.5" style={{ color: 'var(--text-tertiary)' }} />
                <span className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>Tool</span>
              </div>
              <p className="text-xs font-mono pl-5.5" style={{ color: 'var(--accent-purple)' }}>
                {log.tool_name || '—'}
              </p>

              {log.api_key_id && (
                <>
                  <div className="flex items-center gap-2 mt-3">
                    <FileText className="h-3.5 w-3.5" style={{ color: 'var(--text-tertiary)' }} />
                    <span className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>API Key ID</span>
                  </div>
                  <p className="text-xs font-mono pl-5.5" style={{ color: 'var(--text-secondary)' }}>
                    {log.api_key_id}
                  </p>
                </>
              )}
            </div>

            {/* Right column: request/response */}
            <div className="space-y-3">
              {log.request_body && (
                <>
                  <p className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>Request Body</p>
                  <pre className="text-xs font-mono p-3 rounded-lg overflow-auto max-h-32"
                       style={{ background: 'rgba(0,0,0,0.03)', color: 'var(--text-secondary)' }}>
                    {tryFormatJson(log.request_body)}
                  </pre>
                </>
              )}
              {log.response_body && (
                <>
                  <p className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>Response Preview</p>
                  <pre className="text-xs font-mono p-3 rounded-lg overflow-auto max-h-32"
                       style={{ background: 'rgba(0,0,0,0.03)', color: 'var(--text-secondary)' }}>
                    {truncate(tryFormatJson(log.response_body), 100)}
                  </pre>
                </>
              )}
              {!log.request_body && !log.response_body && (
                <p className="text-xs py-4" style={{ color: 'var(--text-tertiary)' }}>
                  No request/response data recorded
                </p>
              )}
            </div>
          </div>
        </div>
      </td>
    </tr>
  )
}

function tryFormatJson(str: string): string {
  try { return JSON.stringify(JSON.parse(str), null, 2) } catch { return str }
}

function truncate(str: string, max: number): string {
  return str.length > max ? str.slice(0, max) + '\n... (truncated)' : str
}

export default function SearchHistory() {
  const [logs, setLogs] = useState<SearchLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [ipFilter, setIpFilter] = useState('')
  const [queryFilter, setQueryFilter] = useState('')
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const load = useCallback(() => {
    const params: Record<string, string> = { page: String(page), page_size: '20' }
    if (ipFilter) params.ip = ipFilter
    if (queryFilter) params.query = queryFilter
    api.getSearchLogs(params).then((r) => { setLogs(r.items); setTotal(r.total) }).catch(() => {})
  }, [page, ipFilter, queryFilter])

  useEffect(() => { load() }, [load])

  const totalPages = Math.ceil(total / 20) || 1

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <Search className="h-6 w-6" style={{ color: 'var(--accent-blue)' }} />
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)' }}>Search History</h1>
        <span className="glass-badge ml-auto" style={{ background: 'rgba(0,122,255,0.08)', color: 'var(--accent-blue)' }}>
          {total.toLocaleString()} total
        </span>
      </div>

      {/* Filters */}
      <div className="glass p-4">
        <div className="flex items-center gap-3">
          <Filter className="h-4 w-4" style={{ color: 'var(--text-tertiary)' }} />
          <input
            className="glass-input px-4 py-2 text-sm flex-1"
            placeholder="Filter by query..."
            value={queryFilter}
            onChange={(e) => { setQueryFilter(e.target.value); setPage(1) }}
          />
          <input
            className="glass-input px-4 py-2 text-sm w-48"
            placeholder="Filter by IP..."
            value={ipFilter}
            onChange={(e) => { setIpFilter(e.target.value); setPage(1) }}
          />
        </div>
      </div>

      {/* Table */}
      <div className="glass overflow-hidden">
        <table className="glass-table">
          <thead>
            <tr>
              <th style={{ width: '32px' }}></th>
              <th>Query</th>
              <th>Engine</th>
              <th>Tool</th>
              <th>IP</th>
              <th>Status</th>
              <th>Latency</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            {logs.length === 0 && (
              <tr>
                <td colSpan={8} className="text-center py-12" style={{ color: 'var(--text-tertiary)' }}>
                  No search logs found
                </td>
              </tr>
            )}
            {logs.map((l) => (
              <Fragment key={l.id}>
                <tr className="cursor-pointer" onClick={() => setExpandedId(expandedId === l.id ? null : l.id)}>
                  <td className="!pr-0">
                    {expandedId === l.id
                      ? <ChevronDown className="h-3.5 w-3.5" style={{ color: 'var(--accent-blue)' }} />
                      : <ChevronRight className="h-3.5 w-3.5" style={{ color: 'var(--text-tertiary)' }} />
                    }
                  </td>
                  <td className="font-medium max-w-[280px] truncate">{l.query}</td>
                  <td>
                    {l.engine ? (
                      <span className="glass-badge" style={{ background: 'rgba(175,82,222,0.08)', color: 'var(--accent-purple)' }}>
                        {l.engine}
                      </span>
                    ) : '—'}
                  </td>
                  <td className="text-xs" style={{ color: 'var(--text-secondary)' }}>{l.tool_name || '—'}</td>
                  <td className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{l.ip_address}</td>
                  <td><StatusBadge code={l.status_code} /></td>
                  <td><LatencyBadge ms={l.elapsed_ms} /></td>
                  <td className="whitespace-nowrap" style={{ color: 'var(--text-tertiary)' }}>
                    <div className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {new Date(l.created_at).toLocaleString()}
                    </div>
                  </td>
                </tr>
                {expandedId === l.id && <DetailPanel log={l} />}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between">
        <p className="text-sm" style={{ color: 'var(--text-tertiary)' }}>
          Page {page} of {totalPages}
        </p>
        <div className="flex gap-2">
          <button
            className="glass-btn glass-btn-ghost px-4 py-1.5 text-sm disabled:opacity-40"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </button>
          <button
            className="glass-btn glass-btn-ghost px-4 py-1.5 text-sm disabled:opacity-40"
            disabled={logs.length < 20}
            onClick={() => setPage(page + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  )
}
