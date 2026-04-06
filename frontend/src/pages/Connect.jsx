import { useState, useEffect, useCallback } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import { useNavigate } from 'react-router-dom'

export default function Connect() {
  const navigate = useNavigate()
  const toast = useToast()
  const connect = useGameStore((s) => s.connect)

  // Saved servers state
  const [servers, setServers] = useState([])
  const [serversLoading, setServersLoading] = useState(true)
  const [connectingServerId, setConnectingServerId] = useState(null)
  const [deletingServerId, setDeletingServerId] = useState(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)

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
    try {
      const res = await api.get('/travian/servers')
      setServers(res.data)
    } catch {
      // silent — may be empty
      setServers([])
    } finally {
      setServersLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  async function handleQuickConnect(server) {
    setConnectingServerId(server.id)
    try {
      const res = await api.post(`/travian/servers/${server.id}/connect`)
      const data = res.data
      useGameStore.setState({
        connected: true,
        serverUrl: data.server_url,
        playerName: data.player_name,
        tribeId: data.tribe_id,
        activeVillageId: data.active_village_id,
        villages: data.villages,
      })
      toast.success(`Connected to ${server.label || server.server_url}`)
      navigate('/')
    } catch (err) {
      const message =
        err.response?.data?.detail ||
        err.response?.data?.message ||
        'Failed to connect to server'
      toast.error(message)
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
    try {
      await connect(serverUrl.trim(), travianUsername.trim(), travianPassword)

      if (saveCredentials) {
        try {
          await api.post('/travian/servers', {
            server_url: serverUrl.trim(),
            username: travianUsername.trim(),
            password: travianPassword,
            label: label.trim() || undefined,
          })
        } catch {
          // non-critical — connection succeeded even if save failed
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
    } finally {
      setFormLoading(false)
    }
  }

  function formatDate(dateStr) {
    if (!dateStr) return 'Never'
    const d = new Date(dateStr)
    return d.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  return (
    <div
      className="min-h-screen px-4 py-8"
      style={{ backgroundColor: 'var(--bg-base)' }}
    >
      <div className="max-w-5xl mx-auto">
        {/* Header */}
        <h1
          className="text-2xl mb-6"
          style={{ fontFamily: "'Cinzel', serif", color: 'var(--accent-gold)' }}
        >
          Server Connection
        </h1>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Saved Servers */}
          <div>
            <h2
              className="text-lg mb-4"
              style={{ fontFamily: "'Cinzel', serif", color: 'var(--text-primary)' }}
            >
              Saved Servers
            </h2>

            {serversLoading ? (
              <div className="card flex items-center justify-center py-8">
                <span
                  className="inline-block w-6 h-6 rounded-full animate-spin"
                  style={{
                    border: '2px solid var(--accent-gold)',
                    borderTopColor: 'transparent',
                  }}
                />
                <span className="ml-3" style={{ color: 'var(--text-secondary)' }}>
                  Loading servers...
                </span>
              </div>
            ) : servers.length === 0 ? (
              <div className="card text-center py-8">
                <p style={{ color: 'var(--text-secondary)' }}>No saved servers yet</p>
                <p className="text-sm mt-1" style={{ color: 'var(--text-secondary)', opacity: 0.7 }}>
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
                          <div
                            className="font-semibold text-sm mb-1"
                            style={{ color: 'var(--accent-gold)' }}
                          >
                            {server.label}
                          </div>
                        )}
                        <div
                          className="text-sm truncate"
                          style={{ color: 'var(--text-primary)' }}
                          title={server.server_url}
                        >
                          {server.server_url}
                        </div>
                        <div
                          className="text-xs mt-1"
                          style={{ color: 'var(--text-secondary)' }}
                        >
                          {server.username}
                        </div>
                        <div
                          className="text-xs mt-0.5"
                          style={{ color: 'var(--text-secondary)', opacity: 0.7 }}
                        >
                          Last connected: {formatDate(server.last_connected)}
                        </div>
                      </div>
                      <div className="flex flex-col gap-2 flex-shrink-0">
                        <button
                          className="btn-primary text-sm"
                          style={{ padding: '0.375rem 0.75rem' }}
                          onClick={() => handleQuickConnect(server)}
                          disabled={connectingServerId === server.id}
                        >
                          {connectingServerId === server.id ? (
                            <span className="flex items-center gap-1.5">
                              <span
                                className="inline-block w-3 h-3 rounded-full animate-spin"
                                style={{
                                  border: '2px solid var(--bg-base)',
                                  borderTopColor: 'transparent',
                                }}
                              />
                              Connecting...
                            </span>
                          ) : (
                            'Connect'
                          )}
                        </button>
                        {confirmDeleteId === server.id ? (
                          <div className="flex gap-1">
                            <button
                              className="btn-danger text-xs"
                              style={{ padding: '0.25rem 0.5rem' }}
                              onClick={() => handleDeleteServer(server.id)}
                              disabled={deletingServerId === server.id}
                            >
                              {deletingServerId === server.id ? '...' : 'Yes'}
                            </button>
                            <button
                              className="btn-secondary text-xs"
                              style={{ padding: '0.25rem 0.5rem' }}
                              onClick={() => setConfirmDeleteId(null)}
                            >
                              No
                            </button>
                          </div>
                        ) : (
                          <button
                            className="btn-danger text-xs"
                            style={{ padding: '0.25rem 0.5rem' }}
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
            <h2
              className="text-lg mb-4"
              style={{ fontFamily: "'Cinzel', serif", color: 'var(--text-primary)' }}
            >
              Add New Server
            </h2>

            <div className="card">
              <form onSubmit={handleNewConnect} className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="server-url"
                    className="text-sm font-semibold"
                    style={{ color: 'var(--text-secondary)' }}
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
                    className="text-sm font-semibold"
                    style={{ color: 'var(--text-secondary)' }}
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
                    className="text-sm font-semibold"
                    style={{ color: 'var(--text-secondary)' }}
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
                    className="text-sm font-semibold"
                    style={{ color: 'var(--text-secondary)' }}
                  >
                    Label{' '}
                    <span style={{ color: 'var(--text-secondary)', opacity: 0.6, fontWeight: 400 }}>
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

                <label
                  className="flex items-center gap-2 cursor-pointer select-none"
                  style={{ color: 'var(--text-secondary)' }}
                >
                  <input
                    type="checkbox"
                    checked={saveCredentials}
                    onChange={(e) => setSaveCredentials(e.target.checked)}
                    disabled={formLoading}
                    style={{ accentColor: 'var(--accent-gold)' }}
                  />
                  <span className="text-sm">Save credentials for quick connect</span>
                </label>

                {/* Error message */}
                {formError && (
                  <div
                    className="text-sm px-3 py-2 rounded"
                    style={{
                      backgroundColor: 'rgba(179, 64, 64, 0.2)',
                      border: '1px solid var(--danger)',
                      color: '#e88',
                    }}
                  >
                    {formError}
                  </div>
                )}

                {/* Submit */}
                <button
                  type="submit"
                  className="btn-primary w-full flex items-center justify-center gap-2"
                  disabled={formLoading}
                  style={{ padding: '0.625rem 1rem', fontSize: '1rem' }}
                >
                  {formLoading && (
                    <span
                      className="inline-block w-4 h-4 rounded-full animate-spin"
                      style={{
                        border: '2px solid var(--bg-base)',
                        borderTopColor: 'transparent',
                      }}
                    />
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
