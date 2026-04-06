import { useEffect, lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import useAuthStore from './stores/authStore'
import Layout from './components/Layout'

// Route-based code splitting — each page is a separate chunk
const Login = lazy(() => import('./pages/Login'))
const Connect = lazy(() => import('./pages/Connect'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Buildings = lazy(() => import('./pages/Buildings'))
const Military = lazy(() => import('./pages/Military'))
const Reports = lazy(() => import('./pages/Reports'))
const VideoRewards = lazy(() => import('./pages/VideoRewards'))
const FarmLists = lazy(() => import('./pages/FarmLists'))
const AutoScout = lazy(() => import('./pages/AutoScout'))
const BuildQueue = lazy(() => import('./pages/BuildQueue'))

function LoadingScreen() {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-base gap-4">
      <span className="logo-text-lg">Travian Auto Player</span>
      <div className="spinner spinner-lg" />
    </div>
  )
}

function PageLoader() {
  return (
    <div className="flex items-center justify-center py-16">
      <div className="spinner spinner-md" />
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

  if (!initialCheckDone) {
    return <LoadingScreen />
  }

  return (
    <Suspense fallback={<PageLoader />}>
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
    </Suspense>
  )
}
