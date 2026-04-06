import { useEffect, lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import useAuthStore from './stores/authStore'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'

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
const Logs = lazy(() => import('./pages/Logs'))

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

function GuardedPage({ children }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<PageLoader />}>
        {children}
      </Suspense>
    </ErrorBoundary>
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
    <ErrorBoundary>
      <Suspense fallback={<PageLoader />}>
        <Routes>
          <Route path="/login" element={isAuthenticated ? <Navigate to="/connect" replace /> : <GuardedPage><Login /></GuardedPage>} />
          <Route path="/connect" element={isAuthenticated ? <GuardedPage><Connect /></GuardedPage> : <Navigate to="/login" replace />} />
          <Route element={isAuthenticated ? <Layout /> : <Navigate to="/login" replace />}>
            <Route path="/" element={<GuardedPage><Dashboard /></GuardedPage>} />
            <Route path="/buildings" element={<GuardedPage><Buildings /></GuardedPage>} />
            <Route path="/military" element={<GuardedPage><Military /></GuardedPage>} />
            <Route path="/reports" element={<GuardedPage><Reports /></GuardedPage>} />
            <Route path="/video" element={<GuardedPage><VideoRewards /></GuardedPage>} />
            <Route path="/farm" element={<GuardedPage><FarmLists /></GuardedPage>} />
            <Route path="/scout" element={<GuardedPage><AutoScout /></GuardedPage>} />
            <Route path="/queue" element={<GuardedPage><BuildQueue /></GuardedPage>} />
            <Route path="/logs" element={<GuardedPage><Logs /></GuardedPage>} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  )
}
