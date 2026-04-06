import { useEffect, useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import useAuthStore from '../stores/authStore'
import useGameStore from '../stores/gameStore'
import ToastContainer from './Toast'

const navItems = [
  { to: '/', label: 'Dashboard', icon: '⌂' },
  { to: '/buildings', label: 'Buildings', icon: '🏛' },
  { to: '/military', label: 'Military', icon: '⚔' },
  { to: '/reports', label: 'Reports', icon: '📜' },
  { to: '/video', label: 'Video Rewards', icon: '🎬' },
  { to: '/farm', label: 'Farm Lists', icon: '🌾' },
  { to: '/scout', label: 'Auto Scout', icon: '🔭' },
  { to: '/queue', label: 'Build Queue', icon: '📋' },
]

export default function Layout() {
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const fetchUser = useAuthStore((s) => s.fetchUser)
  const connected = useGameStore((s) => s.connected)
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const checkStatus = useGameStore((s) => s.checkStatus)
  const disconnect = useGameStore((s) => s.disconnect)

  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    if (!user) fetchUser()
    checkStatus()
  }, [])

  useEffect(() => {
    if (!connected) {
      navigate('/connect', { replace: true })
    }
  }, [connected, navigate])

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  const handleDisconnect = async () => {
    await disconnect()
    navigate('/connect', { replace: true })
  }

  const sidebarWidth = 220
  const topBarHeight = 56

  return (
    <div style={{ minHeight: '100vh', backgroundColor: 'var(--bg-base)' }}>
      <ToastContainer />

      {/* Top Bar */}
      <header
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          height: `${topBarHeight}px`,
          backgroundColor: 'var(--bg-surface)',
          borderBottom: '1px solid var(--border)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 1rem',
          zIndex: 100,
        }}
      >
        {/* Left: Logo + mobile toggle */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          {connected && (
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--text-primary)',
                fontSize: '1.3rem',
                cursor: 'pointer',
                display: 'none',
              }}
              className="sidebar-toggle"
            >
              ☰
            </button>
          )}
          <span
            style={{
              fontFamily: "'Cinzel Decorative', serif",
              fontSize: '1.1rem',
              color: 'var(--accent-gold)',
              fontWeight: 700,
              whiteSpace: 'nowrap',
            }}
          >
            Travian Auto Player
          </span>
        </div>

        {/* Center: Connection info */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.75rem',
            fontSize: '0.85rem',
            color: 'var(--text-secondary)',
          }}
        >
          {connected && serverUrl && (
            <>
              <span
                style={{
                  width: '8px',
                  height: '8px',
                  borderRadius: '50%',
                  backgroundColor: 'var(--success)',
                  display: 'inline-block',
                  flexShrink: 0,
                }}
              />
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '250px' }}>
                {serverUrl}
              </span>
              {playerName && (
                <span style={{ color: 'var(--accent-gold)', fontWeight: 600 }}>
                  {playerName}
                </span>
              )}
            </>
          )}
        </div>

        {/* Right: User + logout */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          {user && (
            <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              {user.username}
            </span>
          )}
          <button
            onClick={handleLogout}
            className="btn-secondary"
            style={{ padding: '0.3rem 0.75rem', fontSize: '0.8rem' }}
          >
            Logout
          </button>
        </div>
      </header>

      {/* Sidebar */}
      {connected && (
        <>
          {/* Mobile overlay */}
          {sidebarOpen && (
            <div
              onClick={() => setSidebarOpen(false)}
              className="sidebar-overlay"
              style={{
                position: 'fixed',
                inset: 0,
                backgroundColor: 'rgba(0,0,0,0.5)',
                zIndex: 149,
                display: 'none',
              }}
            />
          )}

          <aside
            className={sidebarOpen ? 'sidebar sidebar-open' : 'sidebar'}
            style={{
              position: 'fixed',
              top: `${topBarHeight}px`,
              left: 0,
              bottom: 0,
              width: `${sidebarWidth}px`,
              backgroundColor: 'var(--bg-surface)',
              borderRight: '1px solid var(--border)',
              display: 'flex',
              flexDirection: 'column',
              zIndex: 150,
              overflowY: 'auto',
            }}
          >
            <nav style={{ flex: 1, padding: '0.5rem 0' }}>
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  onClick={() => setSidebarOpen(false)}
                  style={({ isActive }) => ({
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.75rem',
                    padding: '0.65rem 1rem',
                    color: isActive ? 'var(--accent-gold)' : 'var(--text-secondary)',
                    textDecoration: 'none',
                    fontSize: '0.9rem',
                    borderLeft: isActive ? '3px solid var(--accent-gold)' : '3px solid transparent',
                    backgroundColor: isActive ? 'rgba(201, 168, 76, 0.08)' : 'transparent',
                    transition: 'all 0.15s',
                  })}
                >
                  <span style={{ fontSize: '1.1rem', width: '1.5rem', textAlign: 'center' }}>
                    {item.icon}
                  </span>
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </nav>

            {/* Bottom: disconnect + status */}
            <div
              style={{
                padding: '0.75rem 1rem',
                borderTop: '1px solid var(--border)',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  marginBottom: '0.75rem',
                  fontSize: '0.8rem',
                }}
              >
                <span
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    backgroundColor: 'var(--success)',
                    display: 'inline-block',
                  }}
                />
                <span style={{ color: 'var(--text-secondary)' }}>Connected</span>
              </div>
              <button
                onClick={handleDisconnect}
                className="btn-danger"
                style={{
                  width: '100%',
                  fontSize: '0.8rem',
                  padding: '0.4rem 0.75rem',
                }}
              >
                Disconnect
              </button>
            </div>
          </aside>
        </>
      )}

      {/* Main content */}
      <main
        style={{
          marginTop: `${topBarHeight}px`,
          marginLeft: connected ? `${sidebarWidth}px` : 0,
          minHeight: `calc(100vh - ${topBarHeight}px)`,
          transition: 'margin-left 0.2s',
        }}
        className="main-content"
      >
        <Outlet />
      </main>

      {/* Responsive styles */}
      <style>{`
        @media (max-width: 768px) {
          .sidebar-toggle {
            display: block !important;
          }
          .sidebar {
            transform: translateX(-100%);
            transition: transform 0.2s ease;
          }
          .sidebar-open {
            transform: translateX(0) !important;
          }
          .sidebar-overlay {
            display: block !important;
          }
          .main-content {
            margin-left: 0 !important;
          }
        }
      `}</style>
    </div>
  )
}
