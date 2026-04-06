import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let logoutTriggered = false;

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && !logoutTriggered) {
      const hadToken = !!localStorage.getItem('token');
      localStorage.removeItem('token');

      if (hadToken) {
        logoutTriggered = true;
        import('./stores/authStore')
          .then(({ default: useAuthStore }) => {
            const state = useAuthStore.getState();
            if (state.isAuthenticated) {
              state.logout();
            }
          })
          .catch(() => {})
          .finally(() => { logoutTriggered = false; });
      }
    }
    return Promise.reject(error);
  }
);

export default api;
