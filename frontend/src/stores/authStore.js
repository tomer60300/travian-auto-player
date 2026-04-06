import { create } from 'zustand';
import api from '../api';

const useAuthStore = create((set, get) => ({
  token: localStorage.getItem('token'),
  user: null,
  isAuthenticated: !!localStorage.getItem('token'),

  login: async (username, password) => {
    const res = await api.post('/users/login', { username, password });
    const { access_token } = res.data;
    localStorage.setItem('token', access_token);
    set({ token: access_token, isAuthenticated: true });
    await get().fetchUser();
  },

  register: async (username, password) => {
    const res = await api.post('/users/register', { username, password });
    const { access_token } = res.data;
    localStorage.setItem('token', access_token);
    set({ token: access_token, isAuthenticated: true });
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

  logout: () => {
    localStorage.removeItem('token');
    set({ token: null, user: null, isAuthenticated: false });
  },
}));

export default useAuthStore;
