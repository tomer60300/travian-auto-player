import { useEffect, useState } from 'react'
import { Outlet, NavLink, useNavigate, useLocation } from 'react-router-dom'
import useAuthStore from '../stores/authStore'
import useGameStore from '../stores/gameStore'
import useLogStore from '../stores/logStore'
import { connectLogStream, disconnectLogStream } from '../logStream'
import { useToast } from './Toast'
import MobileNav from './MobileNav'
import VillageSelector from './VillageSelector'
// ToastContainer is mounted in App.jsx (global, works for all routes)

const navItems = [
  { to: '/', label: 'Dashboard', icon: '⌂' },
  { to: '/buildings', label: 'Buildings', icon: '🏛' },
  { to: '/military', label: 'Military', icon: '⚔' },
  { to: '/reports', label: 'Reports', icon: '📜' },
  { to: '/video', label: 'Video Rewards', icon: '🎬' },
  { to: '/farm', label: 'Farm Lists', icon: '🌾' },
  { to: '/farm-builder', label: 'Farm Builder', icon: '🔨' },
  { to: '/scout', label: 'Auto Scout', icon: '🔭' },
  { to: '/oasis-raider', label: 'Oasis Raider', icon: '🏕' },
  { to: '/raid-optimizer', label: 'Raid Optimizer', icon: '🧮' },
  { to: '/resource-planner', label: 'Resource Planner', icon: '⚖️' },
  { to: '/queue', label: 'Build Queue', icon: '📋' },
  { to: '/logs', label: 'Activity Log', icon: '📊' },
  { to: '/sessions', label: 'Sessions', icon: '📡' },
]

export default function Layout() {
  const navigate = useNavigate()
  const location = useLocation()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const connected = useGameStore((s) => s.connected)
  const serverLogCount = useLogStore((s) => s.serverLogCount)
  const resetServerLogCount = useLogStore((s) => s.resetServerLogCount)
  const statusChecked = useGameStore((s) => s.statusChecked)
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const checkStatus = useGameStore((s) => s.checkStatus)
  const disconnect = useGameStore((s) => s.disconnect)
  const toast = useToast()

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  // Connect log stream when authenticated and connected
  useEffect(() => {
    if (connected) {
      connectLogStream()
    }
    return () => disconnectLogStream()
  }, [connected])

  // Reset server log count when viewing logs page
  useEffect(() => {
    if (location.pathname === '/logs') {
      resetServerLogCount()
    }
  }, [location.pathname, resetServerLogCount])

  // Check Travian connection status once on mount
  useEffect(() => {
    if (!statusChecked) {
      checkStatus()
    }
  }, [statusChecked, checkStatus])

  // Redirect to /connect only on INITIAL status check — not on subsequent poll failures.
  // This prevents all open tabs from being kicked to /connect when one disconnects.
  const [initialRedirectDone, setInitialRedirectDone] = useState(false)
  useEffect(() => {
    if (statusChecked && !connected && !initialRedirectDone && location.pathname !== '/connect') {
      setInitialRedirectDone(true)
      navigate('/connect', { replace: true })
    }
    if (statusChecked && connected) {
      setInitialRedirectDone(true)
    }
  }, [statusChecked, connected, navigate, location.pathname, initialRedirectDone])

  // Add 60s health poll when connected
  useEffect(() => {
    if (!connected) return
    const id = setInterval(() => {
      checkStatus()
    }, 60000)
    return () => clearInterval(id)
  }, [connected, checkStatus])

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  const handleDisconnect = async () => {
    try {
      await disconnect()
    } catch (err) {
      // Typically a 409: operations are still running and the backend kept
      // the session alive. Navigating to /connect would lie about the state.
      toast.error(err.response?.data?.detail || 'Disconnect failed — still connected')
      return
    }
    navigate('/connect', { replace: true })
  }

  // Show loading while checking connection status
  if (!statusChecked) {
    return (
      // Reserves the fixed top bar's 64px, the same way <main> does below and
      // for the same reason: see the comment there.
      <div className="min-h-screen pt-[64px] flex flex-col items-center justify-center bg-base gap-4">
        <span className="text-secondary text-sm">
          Checking connection...
        </span>
        <div className="spinner spinner-md" />
      </div>
    )
  }

  const sidebarWidth = sidebarCollapsed ? 60 : 220

  return (
    <div className="min-h-screen bg-base">
      {/* MD3 Atmospheric Background Shapes */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none" aria-hidden="true" style={{ zIndex: 0 }}>
        <div className="md3-blur-shape md3-blur-primary" style={{ width: 600, height: 600, top: -200, right: -100 }} />
        <div className="md3-blur-shape md3-blur-secondary" style={{ width: 500, height: 500, bottom: -150, left: -100 }} />
        <div className="md3-blur-shape md3-blur-tertiary" style={{ width: 400, height: 400, top: '40%', left: '50%', transform: 'translateX(-50%)' }} />
      </div>

      {/* Top Bar */}
      <header className="top-bar">
        {/* Left section */}
        <div className="flex items-center gap-3">
          {/* Hamburger: only visible on mobile when sidebar overlay is used (hidden by md:hidden) */}
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

        {/* Center: Village selector on mobile */}
        {connected && (
          <div className="mobile-only flex items-center">
            <VillageSelector compact />
          </div>
        )}

        {/* Right section - desktop */}
        <div className="desktop-only flex items-center gap-3 text-sm text-secondary">
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

        {/* Right section - mobile: connection status dot + logout */}
        <div className="mobile-only flex items-center gap-2">
          {connected && (
            <span className="status-dot status-dot-success" />
          )}
          <button
            onClick={handleLogout}
            className="btn-secondary btn-sm"
          >
            Logout
          </button>
        </div>
      </header>

      {/* Sidebar — hidden on mobile via CSS (.sidebar has display:none at max-width:767px) */}
      {connected && (
        <>
          {sidebarOpen && (
            <div
              onClick={() => setSidebarOpen(false)}
              className="sidebar-overlay fixed inset-0 bg-black/40 backdrop-blur-sm z-[149] md:hidden"
            />
          )}

          <aside
            className={sidebarOpen ? 'sidebar sidebar-open' : 'sidebar'}
            style={{ width: sidebarWidth, transition: 'width 200ms ease', willChange: 'width' }}
          >
            {/* Village selector + collapse toggle */}
            <div className="flex items-center gap-1 px-2 py-1.5" style={{ overflow: 'hidden' }}>
              {sidebarCollapsed ? null : (
                <div className="flex-1 min-w-0 px-1">
                  <VillageSelector />
                </div>
              )}
              <button
                onClick={() => setSidebarCollapsed((c) => !c)}
                className="link-action bg-transparent border-none text-secondary cursor-pointer hover:text-primary"
                style={{
                  fontSize: '0.85rem',
                  width: 28,
                  height: 28,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 6,
                  flexShrink: 0,
                }}
                title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              >
                {sidebarCollapsed ? '»' : '«'}
              </button>
            </div>

            {/* Navigation */}
            <nav className="flex-1 py-1" style={{ overflow: 'hidden' }}>
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  onClick={() => setSidebarOpen(false)}
                  className={({ isActive }) => isActive ? 'nav-link nav-link-active' : 'nav-link'}
                  title={sidebarCollapsed ? item.label : undefined}
                  style={sidebarCollapsed ? { justifyContent: 'center', paddingLeft: 0, paddingRight: 0 } : undefined}
                >
                  <span className="nav-icon">
                    {item.icon}
                  </span>
                  {!sidebarCollapsed && <span>{item.label}</span>}
                  {!sidebarCollapsed && item.to === '/logs' && serverLogCount > 0 && (
                    <span className="ml-auto bg-danger text-white text-[10px] font-bold rounded-full min-w-[18px] h-[18px] flex items-center justify-center px-1">
                      {serverLogCount > 99 ? '99+' : serverLogCount}
                    </span>
                  )}
                  {sidebarCollapsed && item.to === '/logs' && serverLogCount > 0 && (
                    <span
                      style={{
                        position: 'absolute',
                        top: 4,
                        right: 4,
                        background: 'var(--danger)',
                        color: '#fff',
                        fontSize: 10,
                        fontWeight: 700,
                        borderRadius: 9999,
                        minWidth: 16,
                        height: 16,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        padding: '0 3px',
                      }}
                    >
                      {serverLogCount > 9 ? '9+' : serverLogCount}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>

            {/* Bottom section */}
            <div className="px-3 py-2 border-t-default" style={{ overflow: 'hidden' }}>
              {!sidebarCollapsed && (
                <div className="flex items-center gap-2">
                  <span className="status-dot status-dot-success" />
                  <button
                    onClick={() => navigate('/connect')}
                    className="text-xs text-secondary hover:text-primary bg-transparent border-none cursor-pointer"
                    style={{ padding: 0 }}
                  >
                    Switch
                  </button>
                  <span className="text-xs text-secondary">·</span>
                  <button
                    onClick={handleDisconnect}
                    className="text-xs bg-transparent border-none cursor-pointer"
                    style={{ padding: 0, color: 'var(--danger)' }}
                  >
                    Disconnect
                  </button>
                </div>
              )}
              {sidebarCollapsed && (
                <div className="flex flex-col items-center gap-1">
                  <span className="status-dot status-dot-success" />
                  <button
                    onClick={() => navigate('/connect')}
                    className="bg-transparent border-none text-secondary cursor-pointer hover:text-primary"
                    title="Switch Server"
                    style={{ fontSize: '0.85rem', padding: 2 }}
                  >
                    🔄
                  </button>
                  <button
                    onClick={handleDisconnect}
                    className="bg-transparent border-none cursor-pointer hover:text-primary"
                    title="Disconnect"
                    style={{ fontSize: '0.85rem', padding: 2, color: 'var(--danger)' }}
                  >
                    ⏏
                  </button>
                </div>
              )}
            </div>
          </aside>
        </>
      )}

      {/* Main content */}
      {/* `pt-[64px]`, not `mt-[64px]`. A top MARGIN here collapsed straight
          out through the layout root and #root to BODY, so the document itself
          sat at y=0 until React mounted and then jumped to y=64: measured as
          BODY moving [0,900] -> [64,836], worth 0.0711 of CLS at 375/768 and
          0.0444 at 1440 on every one of the 13 pages this layout wraps, before
          any page had rendered anything of its own. It was the single largest
          shift on all three of the pages the census flagged, and it was not
          theirs. Padding does not collapse; with `border-box` the box measures
          exactly what the margin version measured. */}
      <main
        className={`main-content min-h-screen pt-[64px] transition-[margin-left] duration-200 ${connected ? '' : 'ml-0'}`}
        style={connected ? { marginLeft: sidebarWidth } : undefined}
      >
        <Outlet />
      </main>

      {/* Mobile bottom tab bar — hidden on desktop via .mobile-only.
          AFTER <main> on purpose. `.bottom-tab-bar` is `position: fixed` at the
          BOTTOM of the viewport, so DOM order here decides tab order and
          nothing else: rendered before <main>, Tab left the top bar for the
          bottom bar and then jumped back up to the page's first control — one
          backward jump on every one of the 13 pages this layout wraps, at
          every width ≤767px. */}
      {connected && (
        <div className="mobile-only">
          <MobileNav />
        </div>
      )}

      {/* Inline styles for responsive helpers — no external CSS changes needed */}
      <style>{`
        .mobile-only { display: none; }
        .desktop-only { display: flex; }
        @media (max-width: 767px) {
          .mobile-only { display: flex; }
          .desktop-only { display: none !important; }
          .main-content { margin-left: 0 !important; }
        }
      `}</style>
    </div>
  )
}
