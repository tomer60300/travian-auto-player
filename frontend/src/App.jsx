import { useEffect, lazy, Suspense, createContext, useMemo } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import useAuthStore from './stores/authStore'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import ToastContainer from './components/Toast'
import CaptchaAlert from './components/CaptchaAlert'

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
const Sessions = lazy(() => import('./pages/Sessions'))

export const TabContext = createContext(null)

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
  const tabId = useMemo(() => {
    try { return crypto.randomUUID() } catch { return Math.random().toString(36).slice(2) + Date.now().toString(36) }
  }, [])

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  if (!initialCheckDone) {
    return (
      <TabContext.Provider value={tabId}>
        <LoadingScreen />
      </TabContext.Provider>
    )
  }

  return (
    <TabContext.Provider value={tabId}>
      <ErrorBoundary>
        <ToastContainer />
        <CaptchaAlert />
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
              <Route path="/sessions" element={<GuardedPage><Sessions /></GuardedPage>} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </TabContext.Provider>
  )
}
