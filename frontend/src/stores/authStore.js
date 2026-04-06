import { create } from 'zustand';
import api from '../api';

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
    } catch {
      get().logout();
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
    } catch {
      localStorage.removeItem('token');
      set({ token: null, user: null, isAuthenticated: false, initialCheckDone: true });
    }
  },

  logout: () => {
    localStorage.removeItem('token');
    set({ token: null, user: null, isAuthenticated: false, initialCheckDone: true });
  },
}));

export default useAuthStore;
