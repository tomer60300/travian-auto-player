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
const OasisRaider = lazy(() => import('./pages/OasisRaider'))
const FarmBuilder = lazy(() => import('./pages/FarmBuilder'))
const RaidOptimizer = lazy(() => import('./pages/RaidOptimizer'))
const ResourcePlanner = lazy(() => import('./pages/ResourcePlanner'))

export const TabContext = createContext(null)

function LoadingScreen({ retryAttempt = 0, retryLimit = 0 }) {
  // A bare spinner cannot distinguish "starting up" from "the backend is down".
  // Once a retry is in flight, say so and show which attempt this is.
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-base gap-4 p-6 text-center">
      <span className="logo-text-lg">Travian Auto Player</span>
      <div className="spinner spinner-lg" />
      {retryAttempt > 0 && (
        <div className="max-w-sm">
          <p className="text-warning text-sm font-semibold">Server unavailable — restoring session</p>
          <p className="text-secondary text-xs mt-1">
            Retrying (attempt {retryAttempt} of {retryLimit}) · next try in ~5s. Your saved sign-in
            is kept.
          </p>
        </div>
      )}
    </div>
  )
}

function AuthOutageScreen({ onRetry }) {
  // Automatic retries were exhausted while the stored token was KEPT: this is an
  // outage, not a credential rejection, so do not present the ordinary login
  // screen as if the sign-in were invalid.
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-base gap-4 p-6 text-center">
      <span className="logo-text-lg">Travian Auto Player</span>
      <div className="max-w-sm">
        <p className="text-warning text-sm font-semibold">⚠ Cannot reach the server</p>
        <p className="text-secondary text-xs mt-1">
          Your session could not be verified because the backend did not respond. Your saved
          sign-in has NOT been cleared — retry once the server is back.
        </p>
      </div>
      <button className="btn-primary btn-sm" onClick={onRetry}>
        Retry
      </button>
      <a className="text-secondary text-xs underline" href="/login">
        Sign in with a different account
      </a>
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
  const authRetryAttempt = useAuthStore((s) => s.authRetryAttempt)
  const authRetryLimit = useAuthStore((s) => s.authRetryLimit)
  const authOutage = useAuthStore((s) => s.authOutage)
  const retryAuth = useAuthStore((s) => s.retryAuth)
  const tabId = useMemo(() => {
    try { return crypto.randomUUID() } catch { return Math.random().toString(36).slice(2) + Date.now().toString(36) }
  }, [])

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  if (!initialCheckDone) {
    return (
      <TabContext.Provider value={tabId}>
        <LoadingScreen retryAttempt={authRetryAttempt} retryLimit={authRetryLimit} />
      </TabContext.Provider>
    )
  }

  // Outage (retries exhausted, token kept) — explain it and offer Retry rather
  // than routing to /login, which would read as "your credentials were wrong".
  if (authOutage && !isAuthenticated) {
    return (
      <TabContext.Provider value={tabId}>
        <AuthOutageScreen onRetry={retryAuth} />
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
              <Route path="/farm-builder" element={<GuardedPage><FarmBuilder /></GuardedPage>} />
              <Route path="/scout" element={<GuardedPage><AutoScout /></GuardedPage>} />
              <Route path="/oasis-raider" element={<GuardedPage><OasisRaider /></GuardedPage>} />
              <Route path="/raid-optimizer" element={<GuardedPage><RaidOptimizer /></GuardedPage>} />
              <Route path="/resource-planner" element={<GuardedPage><ResourcePlanner /></GuardedPage>} />
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
