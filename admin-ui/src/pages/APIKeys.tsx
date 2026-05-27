import { useEffect, useState, useCallback } from 'react'
import { api, type APIKey, type APIKeyCreated } from '@/lib/api'
import { Key, Plus, Trash2, Copy, Check, RotateCw } from 'lucide-react'

export default function APIKeys() {
  const [keys, setKeys] = useState<APIKey[]>([])
  const [name, setName] = useState('')
  const [limit, setLimit] = useState('0')
  const [created, setCreated] = useState<APIKeyCreated | null>(null)
  const [copied, setCopied] = useState(false)

  const load = useCallback(() => {
    api.getKeys().then(setKeys).catch(() => {})
  }, [])

  useEffect(() => { load() }, [load])

  const handleCreate = async () => {
    if (!name.trim()) return
    const key = await api.createKey({ name: name.trim(), call_limit: parseInt(limit) || 0 })
    setCreated(key)
    setName(''); setLimit('0')
    load()
  }

  const handleRevoke = async (id: string) => {
    await api.deleteKey(id)
    load()
  }

  const handleActivate = async (id: string) => {
    await api.setKeyActive(id, true)
    load()
  }

  const handleCopy = async (text: string) => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <Key className="h-6 w-6" style={{ color: 'var(--accent-purple)' }} />
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)' }}>
          API Keys
        </h1>
      </div>

      {created && (
        <div className="glass p-4 animate-fade-in" style={{ borderColor: 'rgba(52, 199, 89, 0.3)' }}>
          <p className="text-sm font-medium" style={{ color: 'var(--accent-green)' }}>
            Key created successfully. Copy it now — it won't be shown again.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 rounded-lg bg-[rgba(0,0,0,0.04)] p-3 text-sm font-mono break-all select-all">
              {created.key}
            </code>
            <button
              className="glass-btn glass-btn-ghost p-2"
              onClick={() => handleCopy(created.key)}
            >
              {copied ? <Check className="h-4 w-4" style={{ color: 'var(--accent-green)' }} /> : <Copy className="h-4 w-4" />}
            </button>
          </div>
          <button
            className="mt-2 text-xs underline"
            style={{ color: 'var(--text-tertiary)' }}
            onClick={() => setCreated(null)}
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="glass p-4">
        <div className="flex gap-3">
          <input
            className="glass-input px-4 py-2 text-sm flex-1"
            placeholder="Key name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="glass-input px-4 py-2 text-sm w-36"
            type="number"
            placeholder="Limit (0 = ∞)"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
          <button
            className="glass-btn glass-btn-primary px-4 py-2 text-sm flex items-center gap-1.5"
            onClick={handleCreate}
          >
            <Plus className="h-4 w-4" />
            Create Key
          </button>
        </div>
      </div>

      <div className="glass overflow-hidden">
        <table className="glass-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Calls</th>
              <th>Limit</th>
              <th>Status</th>
              <th>Created</th>
              <th style={{ width: '100px' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 && (
              <tr>
                <td colSpan={7} className="text-center py-8" style={{ color: 'var(--text-tertiary)' }}>
                  No API keys
                </td>
              </tr>
            )}
            {keys.map((k) => (
              <tr key={k.id}>
                <td className="font-medium">{k.name}</td>
                <td className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>
                  {k.key_prefix}...
                </td>
                <td>{k.call_count}</td>
                <td>{k.call_limit === 0 ? '∞' : k.call_limit}</td>
                <td>
                  <span
                    className="glass-badge"
                    style={{
                      background: k.is_active ? 'rgba(52, 199, 89, 0.12)' : 'rgba(255, 59, 48, 0.12)',
                      color: k.is_active ? 'var(--accent-green)' : 'var(--accent-red)',
                    }}
                  >
                    {k.is_active ? 'Active' : 'Revoked'}
                  </span>
                </td>
                <td style={{ color: 'var(--text-tertiary)' }}>
                  {new Date(k.created_at).toLocaleString()}
                </td>
                <td>
                  {k.is_active ? (
                    <button
                      className="glass-btn glass-btn-ghost px-2.5 py-1 text-xs flex items-center gap-1"
                      style={{ color: 'var(--accent-red)' }}
                      onClick={() => handleRevoke(k.id)}
                    >
                      <Trash2 className="h-3 w-3" />
                      Revoke
                    </button>
                  ) : (
                    <button
                      className="glass-btn glass-btn-ghost px-2.5 py-1 text-xs flex items-center gap-1"
                      style={{ color: 'var(--accent-green)' }}
                      onClick={() => handleActivate(k.id)}
                    >
                      <RotateCw className="h-3 w-3" />
                      Activate
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
