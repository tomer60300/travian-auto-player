import { create } from 'zustand';
import api from '../api';
import useGameStore from './gameStore';
import useLogStore from './logStore';

// Transient /users/me failures are retried a few times before giving up; a
// backend restart must neither log the user out nor pin the whole UI behind
// the loading gate forever.
const AUTH_RETRY_LIMIT = 3
let authRetryCount = 0

const useAuthStore = create((set, get) => ({
  token: localStorage.getItem('token'),
  user: null,
  // Don't trust localStorage token alone — wait for checkAuth to verify
  isAuthenticated: false,
  initialCheckDone: false,
  // Recovery state, so the shell can explain a transient backend outage instead
  // of showing a bare spinner and then an unexplained login screen: which retry
  // is in flight, and whether automatic retries were exhausted while the stored
  // token was still kept (an outage, NOT invalid credentials).
  authRetryAttempt: 0,
  authRetryLimit: AUTH_RETRY_LIMIT,
  authOutage: false,

  login: async (username, password) => {
    const res = await api.post('/users/login', { username, password });
    const { access_token } = res.data;
    localStorage.setItem('token', access_token);
    set({ token: access_token, isAuthenticated: true, initialCheckDone: true });
    await get().fetchUser();
  },

  register: async (username, password) => {
    const res = await api.post('/users/register', { username, password });
    const { access_token } = res.data;
    localStorage.setItem('token', access_token);
    set({ token: access_token, isAuthenticated: true, initialCheckDone: true });
    await get().fetchUser();
  },

  fetchUser: async () => {
    try {
      const res = await api.get('/users/me');
      set({ user: res.data });
    } catch (e) {
      // Only logout on auth failure, not network errors
      if (e.response?.status === 401 || e.response?.status === 403) {
        get().logout();
      }
    }
  },

  checkAuth: async () => {
    const token = localStorage.getItem('token');
    if (!token) {
      set({ isAuthenticated: false, initialCheckDone: true });
      return;
    }
    try {
      const res = await api.get('/users/me');
      authRetryCount = 0;
      set({
        user: res.data,
        isAuthenticated: true,
        initialCheckDone: true,
        authRetryAttempt: 0,
        authOutage: false,
      });
    } catch (e) {
      // Only discard the token when the server says it is bad; a restart,
      // timeout, or 5xx during the initial check must not log the user out.
      if (e.response?.status === 401 || e.response?.status === 403) {
        authRetryCount = 0;
        localStorage.removeItem('token');
        set({
          token: null,
          user: null,
          isAuthenticated: false,
          initialCheckDone: true,
          authRetryAttempt: 0,
          // A genuine credential rejection is NOT an outage — route to login.
          authOutage: false,
        });
      } else if (authRetryCount < AUTH_RETRY_LIMIT) {
        // Transient failure: auth state is UNKNOWN. Leave it untouched — the
        // loading gate stays up on first load instead of bouncing to /login,
        // an already verified session keeps its pages — and retry shortly.
        // Publish the attempt so the shell can say what it is doing.
        authRetryCount += 1;
        set({ authRetryAttempt: authRetryCount, authOutage: false });
        setTimeout(() => { get().checkAuth() }, 5000);
      } else {
        // Retries exhausted with the token still stored: this is a backend
        // outage, not bad credentials. Stop blocking the UI, but flag the
        // outage so the shell offers an explicit Retry instead of silently
        // presenting the ordinary login screen. A verified session stays put.
        authRetryCount = 0;
        const stillHasToken = !!localStorage.getItem('token');
        if (get().isAuthenticated) {
          set({ initialCheckDone: true, authRetryAttempt: 0, authOutage: false });
        } else {
          set({
            isAuthenticated: false,
            initialCheckDone: true,
            authRetryAttempt: 0,
            authOutage: stillHasToken,
          });
        }
      }
    }
  },

  // Operator-triggered retry after automatic attempts were exhausted.
  retryAuth: async () => {
    authRetryCount = 0;
    set({ authOutage: false, authRetryAttempt: 0, initialCheckDone: false });
    await get().checkAuth();
  },

  logout: () => {
    localStorage.removeItem('token');
    // An explicit logout is never an outage — clear the recovery flags so the
    // normal login screen is shown.
    set({
      token: null,
      user: null,
      isAuthenticated: false,
      initialCheckDone: true,
      authRetryAttempt: 0,
      authOutage: false,
    });
    // Clean up other stores to prevent data leaking to next user
    useGameStore.setState({
      connected: false, serverUrl: null, playerName: null, tribeId: null,
      villages: [], activeVillageId: null, resources: null,
      buildings: [], constructionQueue: [], statusChecked: false,
    });
    useLogStore.getState().clear();
  },
}));

export default useAuthStore;
