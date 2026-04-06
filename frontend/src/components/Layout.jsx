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
  { to: '/logs', label: 'Activity Log', icon: '📊' },
]

export default function Layout() {
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const connected = useGameStore((s) => s.connected)
  const statusChecked = useGameStore((s) => s.statusChecked)
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const checkStatus = useGameStore((s) => s.checkStatus)
  const disconnect = useGameStore((s) => s.disconnect)

  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Check Travian connection status once on mount
  useEffect(() => {
    if (!statusChecked) {
      checkStatus()
    }
  }, [statusChecked, checkStatus])

  // Redirect to /connect only AFTER status check completes and confirms not connected
  useEffect(() => {
    if (statusChecked && !connected) {
      navigate('/connect', { replace: true })
    }
  }, [statusChecked, connected, navigate])

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  const handleDisconnect = async () => {
    await disconnect()
    navigate('/connect', { replace: true })
  }

  // Show loading while checking connection status
  if (!statusChecked) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-base gap-4">
        <span className="text-secondary text-sm">
          Checking connection...
        </span>
        <div className="spinner spinner-md" />
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-base">
      <ToastContainer />

      {/* Top Bar */}
      <header className="top-bar">
        <div className="flex items-center gap-3">
          {connected && (
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="sidebar-toggle bg-transparent border-none text-primary text-xl cursor-pointer md:hidden"
            >
              {'☰'}
            </button>
          )}
          <span className="logo-text">
            Travian Auto Player
          </span>
        </div>

        <div className="flex items-center gap-3 text-sm text-secondary">
          {connected && serverUrl && (
            <>
              <span className="status-dot status-dot-success" />
              <span className="truncate max-w-[250px]">
                {serverUrl}
              </span>
              {playerName && (
                <span className="text-gold font-semibold">
                  {playerName}
                </span>
              )}
            </>
          )}
        </div>

        <div className="flex items-center gap-3">
          {user && (
            <span className="text-sm text-secondary">
              {user.username}
            </span>
          )}
          <button
            onClick={handleLogout}
            className="btn-secondary btn-sm"
          >
            Logout
          </button>
        </div>
      </header>

      {/* Sidebar */}
      {connected && (
        <>
          {sidebarOpen && (
            <div
              onClick={() => setSidebarOpen(false)}
              className="sidebar-overlay fixed inset-0 bg-black/50 z-[149] md:hidden"
            />
          )}

          <aside
            className={sidebarOpen ? 'sidebar sidebar-open' : 'sidebar'}
          >
            <nav className="flex-1 py-2">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  onClick={() => setSidebarOpen(false)}
                  className={({ isActive }) => isActive ? 'nav-link nav-link-active' : 'nav-link'}
                >
                  <span className="nav-icon">
                    {item.icon}
                  </span>
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </nav>

            <div className="px-4 py-3 border-t-default">
              <div className="flex items-center gap-2 mb-3 text-xs">
                <span className="status-dot status-dot-success" />
                <span className="text-secondary">Connected</span>
              </div>
              <button
                onClick={() => navigate('/connect')}
                className="btn-secondary btn-sm btn-full mb-2"
              >
                Switch Server
              </button>
              <button
                onClick={handleDisconnect}
                className="btn-danger btn-sm btn-full"
              >
                Disconnect
              </button>
            </div>
          </aside>
        </>
      )}

      {/* Main content */}
      <main
        className={`main-content min-h-[calc(100vh-56px)] mt-[56px] transition-[margin-left] duration-200 ${connected ? 'ml-[220px]' : 'ml-0'}`}
      >
        <Outlet />
      </main>
    </div>
  )
}
