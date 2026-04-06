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

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)

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
