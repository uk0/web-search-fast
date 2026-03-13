import { useState } from 'react'
import { setToken } from '@/lib/api'
import { Globe } from 'lucide-react'

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [token, setTokenValue] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    setToken(token)
    try {
      const res = await fetch('/admin/api/stats', {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.ok) {
        onLogin()
      } else {
        setError('Invalid admin token')
      }
    } catch {
      setError('Connection failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mesh-bg flex min-h-screen items-center justify-center p-4">
      <form
        onSubmit={handleSubmit}
        className="glass-heavy w-full max-w-sm p-8 space-y-6 animate-fade-in"
      >
        <div className="flex flex-col items-center gap-3">
          <div className="w-14 h-14 rounded-2xl flex items-center justify-center"
               style={{ background: 'linear-gradient(135deg, #007aff, #5856d6)' }}>
            <Globe className="h-7 w-7 text-white" />
          </div>
          <div className="text-center">
            <h1 className="text-xl font-semibold" style={{ color: 'var(--text-primary)' }}>
              Web Search MCP
            </h1>
            <p className="text-sm mt-1" style={{ color: 'var(--text-secondary)' }}>
              Admin Console
            </p>
          </div>
        </div>

        <div className="space-y-3">
          <input
            type="password"
            className="glass-input w-full px-4 py-2.5 text-sm"
            placeholder="Enter admin token"
            value={token}
            onChange={(e) => setTokenValue(e.target.value)}
            autoFocus
          />
          {error && (
            <p className="text-sm text-center animate-fade-in" style={{ color: 'var(--accent-red)' }}>
              {error}
            </p>
          )}
        </div>

        <button
          type="submit"
          disabled={loading || !token}
          className="glass-btn glass-btn-primary w-full py-2.5 text-sm disabled:opacity-50"
        >
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
      </form>
    </div>
  )
}
