import { useState } from 'react'
import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import Dashboard from '@/pages/Dashboard'
import SearchHistory from '@/pages/SearchHistory'
import IPMonitor from '@/pages/IPMonitor'
import APIKeys from '@/pages/APIKeys'
import ProxyManager from '@/pages/ProxyManager'
import Login from '@/pages/Login'
import { LayoutDashboard, Search, ShieldBan, Key, Network, LogOut, Globe } from 'lucide-react'

function Layout({ children, onLogout }: { children: React.ReactNode; onLogout: () => void }) {
  const links = [
    { to: '/admin/', label: 'Dashboard', icon: LayoutDashboard },
    { to: '/admin/search', label: 'Search History', icon: Search },
    { to: '/admin/ips', label: 'IP Monitor', icon: ShieldBan },
    { to: '/admin/keys', label: 'API Keys', icon: Key },
    { to: '/admin/proxies', label: 'Proxies', icon: Network },
  ]

  return (
    <div className="mesh-bg flex min-h-screen">
      <aside className="glass-sidebar w-60 p-4 flex flex-col sticky top-0 h-screen">
        <div className="flex items-center gap-2.5 px-3 mb-8 mt-2">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center"
               style={{ background: 'linear-gradient(135deg, #007aff, #5856d6)' }}>
            <Globe className="h-4 w-4 text-white" />
          </div>
          <span className="text-[15px] font-semibold" style={{ color: 'var(--text-primary)' }}>
            WSM Admin
          </span>
        </div>

        <nav className="space-y-1 flex-1">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.to === '/admin/'}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 py-2 text-[13px] font-medium transition-all duration-200 ${
                  isActive
                    ? 'glass text-[var(--accent-blue)]'
                    : 'rounded-[var(--glass-radius-xs)] text-[var(--text-secondary)] hover:bg-[rgba(0,0,0,0.04)]'
                }`
              }
            >
              <l.icon className="h-[18px] w-[18px]" />
              {l.label}
            </NavLink>
          ))}
        </nav>

        <button
          onClick={onLogout}
          className="flex items-center gap-2.5 px-3 py-2 text-[13px] font-medium rounded-[var(--glass-radius-xs)] transition-all duration-200 mt-auto"
          style={{ color: 'var(--text-tertiary)' }}
          onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(0,0,0,0.04)'}
          onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
        >
          <LogOut className="h-[18px] w-[18px]" />
          Sign Out
        </button>
      </aside>

      <main className="flex-1 p-8 overflow-auto">
        {children}
      </main>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(() => !!localStorage.getItem('admin_token'))

  if (!authed) return <Login onLogin={() => setAuthed(true)} />

  const handleLogout = () => {
    localStorage.removeItem('admin_token')
    setAuthed(false)
  }

  return (
    <BrowserRouter>
      <Layout onLogout={handleLogout}>
        <Routes>
          <Route path="/admin/" element={<Dashboard />} />
          <Route path="/admin/search" element={<SearchHistory />} />
          <Route path="/admin/ips" element={<IPMonitor />} />
          <Route path="/admin/keys" element={<APIKeys />} />
          <Route path="/admin/proxies" element={<ProxyManager />} />
          <Route path="*" element={<Navigate to="/admin/" replace />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
