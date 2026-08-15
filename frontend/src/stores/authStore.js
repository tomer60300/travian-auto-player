import { create } from 'zustand';
import api from '../api';
import useGameStore from './gameStore';
import useLogStore from './logStore';

const useAuthStore = create((set, get) => ({
  token: localStorage.getItem('token'),
  user: null,
  // Don't trust localStorage token alone — wait for checkAuth to verify
  isAuthenticated: false,
  initialCheckDone: false,

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
      set({ user: res.data, isAuthenticated: true, initialCheckDone: true });
    } catch (e) {
      // Only discard the token when the server says it is bad; a restart,
      // timeout, or 5xx during the initial check must not log the user out.
      if (e.response?.status === 401 || e.response?.status === 403) {
        localStorage.removeItem('token');
        set({ token: null, user: null, isAuthenticated: false, initialCheckDone: true });
      } else {
        // Transient failure: the token was NOT revalidated, so do not enter
        // the authenticated UI on faith — but keep the token and retry until
        // the backend gives a definitive answer, so an outage never costs the
        // user their session.
        set({ isAuthenticated: false, initialCheckDone: true });
        setTimeout(() => { get().checkAuth() }, 5000);
      }
    }
  },

  logout: () => {
    localStorage.removeItem('token');
    set({ token: null, user: null, isAuthenticated: false, initialCheckDone: true });
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
