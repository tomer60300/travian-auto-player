import { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import useAuthStore from './stores/authStore'
import Layout from './components/Layout'
import Login from './pages/Login'
import Connect from './pages/Connect'
import Dashboard from './pages/Dashboard'
import Buildings from './pages/Buildings'
import Military from './pages/Military'
import Reports from './pages/Reports'
import VideoRewards from './pages/VideoRewards'
import FarmLists from './pages/FarmLists'
import AutoScout from './pages/AutoScout'
import BuildQueue from './pages/BuildQueue'

function LoadingScreen() {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: 'var(--bg-base)',
        gap: '1rem',
      }}
    >
      <span
        style={{
          fontFamily: "'Cinzel Decorative', serif",
          fontSize: '1.4rem',
          color: 'var(--accent-gold)',
        }}
      >
        Travian Auto Player
      </span>
      <div
        style={{
          width: '32px',
          height: '32px',
          border: '3px solid var(--border)',
          borderTopColor: 'var(--accent-gold)',
          borderRadius: '50%',
          animation: 'spin 0.8s linear infinite',
        }}
      />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const initialCheckDone = useAuthStore((s) => s.initialCheckDone)
  const checkAuth = useAuthStore((s) => s.checkAuth)

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  // Don't render routes until we know if the token is valid
  if (!initialCheckDone) {
    return <LoadingScreen />
  }

  return (
    <Routes>
      <Route path="/login" element={!isAuthenticated ? <Login /> : <Navigate to="/" replace />} />
      <Route path="/connect" element={isAuthenticated ? <Connect /> : <Navigate to="/login" replace />} />
      <Route element={isAuthenticated ? <Layout /> : <Navigate to="/login" replace />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/buildings" element={<Buildings />} />
        <Route path="/military" element={<Military />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/video" element={<VideoRewards />} />
        <Route path="/farm" element={<FarmLists />} />
        <Route path="/scout" element={<AutoScout />} />
        <Route path="/queue" element={<BuildQueue />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
