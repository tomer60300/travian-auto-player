import { useState, useEffect, useCallback, useRef } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import { useNavigate } from 'react-router-dom'
import ConnectionProgress from '../components/ConnectionProgress'

export default function Connect() {
  const navigate = useNavigate()
  const toast = useToast()
  const connect = useGameStore((s) => s.connect)
  const connectFromSaved = useGameStore((s) => s.connectFromSaved)
  const connected = useGameStore((s) => s.connected)
  const justConnectedRef = useRef(false)

  // Redirect to dashboard only after a NEW connection is made on this page
  useEffect(() => {
    if (connected && justConnectedRef.current) {
      navigate('/', { replace: true })
    }
  }, [connected, navigate])

  // Saved servers state
  const [servers, setServers] = useState([])
  const [serversLoading, setServersLoading] = useState(true)
  const [serversError, setServersError] = useState(null)
  const [connectingServerId, setConnectingServerId] = useState(null)
  const [deletingServerId, setDeletingServerId] = useState(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)

  // Connection progress overlay
  const [connectingTo, setConnectingTo] = useState(null) // server name/url for the progress overlay

  // New server form state
  const [serverUrl, setServerUrl] = useState('')
  const [travianUsername, setTravianUsername] = useState('')
  const [travianPassword, setTravianPassword] = useState('')
  const [label, setLabel] = useState('')
  const [saveCredentials, setSaveCredentials] = useState(true)
  const [formLoading, setFormLoading] = useState(false)
  const [formError, setFormError] = useState('')

  const fetchServers = useCallback(async () => {
    setServersLoading(true)
    setServersError(null)
    try {
      const res = await api.get('/travian/servers')
      setServers(res.data)
    } catch {
      setServers([])
      setServersError('Failed to load saved servers')
    } finally {
      setServersLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  async function handleQuickConnect(server) {
    setConnectingServerId(server.id)
    setConnectingTo(server.label || server.server_url)
    try {
      await connectFromSaved(server.id)
      justConnectedRef.current = true
      toast.success(`Connected to ${server.label || server.server_url}`)
      navigate('/')
    } catch (err) {
      const message =
        err.response?.data?.detail ||
        err.response?.data?.message ||
        'Failed to connect to server'
      toast.error(message)
      setConnectingTo(null)
    } finally {
      setConnectingServerId(null)
    }
  }

  async function handleDeleteServer(serverId) {
    setDeletingServerId(serverId)
    try {
      await api.delete(`/travian/servers/${serverId}`)
      setServers((prev) => prev.filter((s) => s.id !== serverId))
      toast.success('Server removed')
    } catch (err) {
      const message =
        err.response?.data?.detail || 'Failed to delete server'
      toast.error(message)
    } finally {
      setDeletingServerId(null)
      setConfirmDeleteId(null)
    }
  }

  function validateForm() {
    if (!serverUrl.trim()) return 'Server URL is required'
    if (!travianUsername.trim()) return 'Travian username is required'
    if (!travianPassword.trim()) return 'Travian password is required'
    return null
  }

  async function handleNewConnect(e) {
    e.preventDefault()
    setFormError('')

    const validationError = validateForm()
    if (validationError) {
      setFormError(validationError)
      return
    }

    setFormLoading(true)
    setConnectingTo(label.trim() || serverUrl.trim())
    try {
      await connect(serverUrl.trim(), travianUsername.trim(), travianPassword)
      justConnectedRef.current = true

      if (saveCredentials) {
        try {
          await api.post('/travian/servers', {
            server_url: serverUrl.trim(),
            username: travianUsername.trim(),
            password: travianPassword,
            label: label.trim() || undefined,
          })
        } catch {
          toast.warning('Connected, but failed to save credentials')
        }
      }

      toast.success('Connected to Travian server!')
      navigate('/')
    } catch (err) {
      const message =
        err.response?.data?.detail ||
        err.response?.data?.message ||
        'Failed to connect — check URL and credentials'
      setFormError(message)
      setConnectingTo(null)
    } finally {
      setFormLoading(false)
    }
  }

  function relativeDate(dateStr) {
    if (!dateStr) return 'Never'
    const d = new Date(dateStr)
    if (isNaN(d.getTime())) return dateStr
    const diff = Date.now() - d.getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'Just now'
    if (mins < 60) return `${mins}m ago`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours}h ago`
    const days = Math.floor(hours / 24)
    if (days < 7) return `${days}d ago`
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  }

  return (
    <div className="min-h-screen px-4 py-8 bg-base">
      <ConnectionProgress serverName={connectingTo} isActive={!!connectingTo} />
      <div className="max-w-5xl mx-auto">
        {/* Header */}
        <h1 className="heading-gold text-2xl mb-6">
          Server Connection
        </h1>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Saved Servers */}
          <div>
            <h2 className="text-lg mb-4 text-primary">
              Saved Servers
            </h2>

            {serversLoading ? (
              <div className="card flex items-center justify-center py-8">
                <span className="spinner spinner-sm" />
                <span className="ml-3 text-secondary">
                  Loading servers...
                </span>
              </div>
            ) : serversError ? (
              <div className="card text-center py-8">
                <p className="text-warning">{serversError}</p>
                <button className="btn-secondary btn-sm mt-2" onClick={fetchServers}>Retry</button>
              </div>
            ) : servers.length === 0 ? (
              <div className="card text-center py-8">
                <p className="text-secondary">No saved servers yet</p>
                <p className="text-sm mt-1 text-secondary opacity-70">
                  Connect to a server and save your credentials for quick access
                </p>
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {servers.map((server) => (
                  <div key={server.id} className="card">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        {server.label && (
                          <div className="font-semibold text-sm mb-1 text-gold">
                            {server.label}
                          </div>
                        )}
                        <div
                          className="text-sm truncate text-primary"
                          title={server.server_url}
                        >
                          {server.server_url}
                        </div>
                        <div className="text-xs mt-1 text-secondary">
                          {server.username}
                        </div>
                        <div className="text-xs mt-0.5 text-secondary opacity-70">
                          Last connected: {relativeDate(server.last_connected)}
                        </div>
                      </div>
                      <div className="flex flex-col gap-2 flex-shrink-0">
                        <button
                          className="btn-primary btn-sm"
                          onClick={() => handleQuickConnect(server)}
                          disabled={connectingServerId === server.id}
                          style={{ minWidth: 90, justifyContent: 'center', display: 'flex', alignItems: 'center', gap: 6 }}
                        >
                          {connectingServerId === server.id ? (
                            <>
                              <span className="spinner" style={{ width: 14, height: 14, borderWidth: 2 }} />
                              <span>Connecting</span>
                            </>
                          ) : (
                            'Connect'
                          )}
                        </button>
                        {confirmDeleteId === server.id ? (
                          <div className="flex gap-1">
                            <button
                              className="btn-danger btn-xs"
                              onClick={() => handleDeleteServer(server.id)}
                              disabled={deletingServerId === server.id}
                            >
                              {deletingServerId === server.id ? '...' : 'Yes'}
                            </button>
                            <button
                              className="btn-secondary btn-xs"
                              onClick={() => setConfirmDeleteId(null)}
                            >
                              No
                            </button>
                          </div>
                        ) : (
                          <button
                            className="btn-danger btn-xs"
                            onClick={() => setConfirmDeleteId(server.id)}
                          >
                            Delete
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Right: Add New Server */}
          <div>
            <h2 className="text-lg mb-4 text-primary">
              Add New Server
            </h2>

            <div className="card">
              <form onSubmit={handleNewConnect} className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="server-url"
                    className="text-sm font-semibold text-secondary"
                  >
                    Server URL
                  </label>
                  <input
                    id="server-url"
                    type="text"
                    className="input-field"
                    placeholder="https://ts2.x1.europe.travian.com"
                    value={serverUrl}
                    onChange={(e) => setServerUrl(e.target.value)}
                    disabled={formLoading}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="travian-username"
                    className="text-sm font-semibold text-secondary"
                  >
                    Travian Username
                  </label>
                  <input
                    id="travian-username"
                    type="text"
                    className="input-field"
                    placeholder="Your Travian username"
                    value={travianUsername}
                    onChange={(e) => setTravianUsername(e.target.value)}
                    disabled={formLoading}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="travian-password"
                    className="text-sm font-semibold text-secondary"
                  >
                    Travian Password
                  </label>
                  <input
                    id="travian-password"
                    type="password"
                    className="input-field"
                    placeholder="Your Travian password"
                    value={travianPassword}
                    onChange={(e) => setTravianPassword(e.target.value)}
                    disabled={formLoading}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="server-label"
                    className="text-sm font-semibold text-secondary"
                  >
                    Label{' '}
                    <span className="text-secondary opacity-60 font-normal">
                      (optional)
                    </span>
                  </label>
                  <input
                    id="server-label"
                    type="text"
                    className="input-field"
                    placeholder="e.g., Main Account"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    disabled={formLoading}
                  />
                </div>

                <label className="flex items-center gap-2 cursor-pointer select-none text-secondary">
                  <input
                    type="checkbox"
                    checked={saveCredentials}
                    onChange={(e) => setSaveCredentials(e.target.checked)}
                    disabled={formLoading}
                    className="checkbox-gold"
                  />
                  <span className="text-sm">Save credentials for quick connect</span>
                </label>

                {/* Error message */}
                {formError && (
                  <div className="error-box">{formError}</div>
                )}

                {/* Submit */}
                <button
                  type="submit"
                  className="btn-primary btn-lg w-full flex items-center justify-center gap-2"
                  disabled={formLoading}
                >
                  {formLoading && (
                    <span className="spinner spinner-sm" />
                  )}
                  {formLoading ? 'Connecting to Travian server...' : 'Connect'}
                </button>
              </form>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
