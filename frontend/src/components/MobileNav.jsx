/**
 * MobileNav — Bottom tab bar with primary tabs and a "More" sheet for overflow navigation.
 *
 * Props: none (standalone component, reads route and log count from stores).
 */
import { NavLink, useLocation } from 'react-router-dom'
import { useState } from 'react'
import useLogStore from '../stores/logStore'

const primaryTabs = [
  { to: '/', label: 'Home', icon: '⌂' },
  { to: '/buildings', label: 'Build', icon: '🏛' },
  { to: '/farm', label: 'Farm', icon: '🌾' },
  { to: '/scout', label: 'Scout', icon: '🔭' },
]

const moreTabs = [
  { to: '/military', label: 'Military', icon: '⚔' },
  { to: '/reports', label: 'Reports', icon: '📜' },
  { to: '/video', label: 'Video', icon: '🎬' },
  { to: '/queue', label: 'Queue', icon: '📋' },
  { to: '/logs', label: 'Logs', icon: '📊' },
]

export default function MobileNav() {
  const [sheetOpen, setSheetOpen] = useState(false)
  const location = useLocation()
  const serverLogCount = useLogStore((s) => s.serverLogCount)

  // Check if any "more" tab is currently active
  const moreIsActive = moreTabs.some((t) => t.to === location.pathname)

  return (
    <>
      {/* Bottom Tab Bar */}
      <nav className="bottom-tab-bar">
        {primaryTabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === '/'}
            className={({ isActive }) =>
              isActive ? 'tab-active' : ''
            }
            onClick={() => setSheetOpen(false)}
          >
            <span className="tab-icon">{tab.icon}</span>
            <span style={{ fontSize: '0.625rem' }}>{tab.label}</span>
          </NavLink>
        ))}
        <button
          onClick={() => setSheetOpen((prev) => !prev)}
          className={`bg-transparent border-none cursor-pointer text-primary ${moreIsActive || sheetOpen ? 'tab-active' : ''}`}
          aria-label="More navigation options"
        >
          <span className="tab-icon">•••</span>
          <span style={{ fontSize: '0.625rem' }}>More</span>
        </button>
      </nav>

      {/* Bottom Sheet Overlay + Sheet */}
      {sheetOpen && (
        <>
          <div
            className="bottom-sheet-overlay"
            onClick={() => setSheetOpen(false)}
          />
          <div className="bottom-sheet">
            <div style={{ padding: '1rem' }}>
              <div
                style={{
                  width: 40,
                  height: 4,
                  borderRadius: 2,
                  background: 'var(--color-border, #555)',
                  margin: '0 auto 1rem',
                }}
              />
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(3, 1fr)',
                  gap: '0.75rem',
                }}
              >
                {moreTabs.map((tab) => (
                  <NavLink
                    key={tab.to}
                    to={tab.to}
                    onClick={() => setSheetOpen(false)}
                    className={({ isActive }) =>
                      isActive ? 'nav-link nav-link-active' : 'nav-link'
                    }
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      justifyContent: 'center',
                      minHeight: 64,
                      minWidth: 44,
                      borderRadius: 12,
                      textDecoration: 'none',
                      position: 'relative',
                      padding: '0.5rem',
                      borderLeft: 'none',
                    }}
                  >
                    <span style={{ fontSize: '1.5rem' }}>{tab.icon}</span>
                    <span style={{ fontSize: '0.75rem', marginTop: 4 }}>
                      {tab.label}
                    </span>
                    {tab.to === '/logs' && serverLogCount > 0 && (
                      <span
                        style={{
                          position: 'absolute',
                          top: 4,
                          right: 4,
                          background: 'var(--color-danger, #e53e3e)',
                          color: '#fff',
                          fontSize: 10,
                          fontWeight: 700,
                          borderRadius: 9999,
                          minWidth: 18,
                          height: 18,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          padding: '0 4px',
                        }}
                      >
                        {serverLogCount > 99 ? '99+' : serverLogCount}
                      </span>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </>
  )
}
