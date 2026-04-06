import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const hadToken = !!localStorage.getItem('token');
      localStorage.removeItem('token');

      // If there was a token and it's now rejected, force the auth store
      // to update. Import is dynamic to avoid circular deps.
      if (hadToken) {
        import('./stores/authStore').then(({ default: useAuthStore }) => {
          const state = useAuthStore.getState();
          if (state.isAuthenticated) {
            state.logout();
          }
        });
      }
    }
    return Promise.reject(error);
  }
);

export default api;
