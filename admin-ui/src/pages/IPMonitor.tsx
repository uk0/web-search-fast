import { useEffect, useState, useCallback } from 'react'
import { api, type IPBan } from '@/lib/api'
import { ShieldBan, Plus, Trash2 } from 'lucide-react'

export default function IPMonitor() {
  const [bans, setBans] = useState<IPBan[]>([])
  const [ip, setIp] = useState('')
  const [reason, setReason] = useState('')

  const load = useCallback(() => {
    api.getBans().then(setBans).catch(() => {})
  }, [])

  useEffect(() => { load() }, [load])

  const handleBan = async () => {
    if (!ip.trim()) return
    await api.banIP({ ip: ip.trim(), reason })
    setIp(''); setReason('')
    load()
  }

  const handleUnban = async (banIp: string) => {
    await api.unbanIP(banIp)
    load()
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <ShieldBan className="h-6 w-6" style={{ color: 'var(--accent-red)' }} />
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)' }}>
          IP Monitor
        </h1>
      </div>

      <div className="glass p-4">
        <div className="flex gap-3">
          <input
            className="glass-input px-4 py-2 text-sm w-48"
            placeholder="IP address"
            value={ip}
            onChange={(e) => setIp(e.target.value)}
          />
          <input
            className="glass-input px-4 py-2 text-sm flex-1"
            placeholder="Reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            className="glass-btn glass-btn-danger px-4 py-2 text-sm flex items-center gap-1.5"
            onClick={handleBan}
          >
            <Plus className="h-4 w-4" />
            Ban IP
          </button>
        </div>
      </div>

      <div className="glass overflow-hidden">
        <table className="glass-table">
          <thead>
            <tr>
              <th>IP Address</th>
              <th>Reason</th>
              <th>Banned At</th>
              <th style={{ width: '100px' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {bans.length === 0 && (
              <tr>
                <td colSpan={4} className="text-center py-8" style={{ color: 'var(--text-tertiary)' }}>
                  No banned IPs
                </td>
              </tr>
            )}
            {bans.map((b) => (
              <tr key={b.id}>
                <td className="font-mono text-sm">{b.ip_address}</td>
                <td style={{ color: 'var(--text-secondary)' }}>{b.reason || '—'}</td>
                <td style={{ color: 'var(--text-tertiary)' }}>
                  {new Date(b.created_at).toLocaleString()}
                </td>
                <td>
                  <button
                    className="glass-btn glass-btn-ghost px-2.5 py-1 text-xs flex items-center gap-1"
                    style={{ color: 'var(--accent-red)' }}
                    onClick={() => handleUnban(b.ip_address)}
                  >
                    <Trash2 className="h-3 w-3" />
                    Unban
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
