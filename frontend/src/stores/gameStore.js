import { create } from 'zustand';
import api from '../api';

const useGameStore = create((set, get) => ({
  connected: false,
  statusChecked: false,
  serverUrl: null,
  playerName: null,
  tribeId: null,
  villages: [],
  activeVillageId: null,
  resources: null,
  buildings: [],
  buildingsLoading: false,
  buildingsError: null,
  constructionQueue: [],

  connect: async (serverUrl, username, password) => {
    const res = await api.post('/travian/connect', {
      server_url: serverUrl,
      username,
      password,
    });
    const data = res.data;
    set({
      connected: true,
      statusChecked: true,
      serverUrl: data.server_url,
      playerName: data.player_name,
      tribeId: data.tribe_id,
      activeVillageId: data.active_village_id,
      villages: Array.isArray(data.villages) ? data.villages : [],
    });
    return data;
  },

  connectFromSaved: async (serverId) => {
    const res = await api.post(`/travian/servers/${serverId}/connect`);
    const data = res.data;
    set({
      connected: true,
      statusChecked: true,
      serverUrl: data.server_url,
      playerName: data.player_name,
      tribeId: data.tribe_id,
      activeVillageId: data.active_village_id,
      villages: Array.isArray(data.villages) ? data.villages : [],
    });
    return data;
  },

  disconnect: async () => {
    try { await api.delete('/travian/disconnect'); } catch (e) { console.warn('Disconnect failed:', e) }
    set({
      connected: false,
      serverUrl: null,
      playerName: null,
      tribeId: null,
      villages: [],
      activeVillageId: null,
      resources: null,
      buildings: [],
      constructionQueue: [],
    });
  },

  checkStatus: async () => {
    try {
      const res = await api.get('/travian/status');
      const data = res.data;
      if (data && data.connected) {
        set({
          connected: true,
          statusChecked: true,
          serverUrl: data.server_url,
          playerName: data.player_name,
          tribeId: data.tribe_id,
          activeVillageId: data.active_village_id,
          villages: Array.isArray(data.villages) ? data.villages : [],
        });
      } else {
        set({ connected: false, statusChecked: true });
      }
    } catch (e) { console.warn('Store fetch failed:', e)
      set({ connected: false, statusChecked: true });
    }
  },

  switchVillage: async (villageId) => {
    await api.post('/villages/switch', { village_id: villageId });
    set({ activeVillageId: villageId });
    await Promise.all([get().fetchResources(), get().fetchBuildings()]);
  },

  // Helper: if a 403 means "not connected to Travian", verify with checkStatus
  _handleFetchError: (e) => {
    const status = e.response?.status;
    if (status === 403) {
      const current = useGameStore.getState();
      if (current.connected) {
        // Don't immediately disconnect — verify the session is actually gone
        get().checkStatus();
      }
    }
  },

  fetchResources: async () => {
    try {
      const res = await api.get('/buildings/resources');
      if (res.data && typeof res.data === 'object' && !Array.isArray(res.data)) {
        set({ resources: res.data });
      }
    } catch (e) {
      console.warn('fetchResources failed:', e);
      get()._handleFetchError(e);
    }
  },

  fetchBuildings: async () => {
    set({ buildingsLoading: true, buildingsError: null });
    try {
      const res = await api.get('/buildings');
      // API returns { village_id, buildings: [...] } or possibly a plain array
      const arr = Array.isArray(res.data) ? res.data
        : Array.isArray(res.data?.buildings) ? res.data.buildings
        : [];
      set({ buildings: arr, buildingsLoading: false });
    } catch (e) {
      console.warn('fetchBuildings failed:', e);
      const status = e.response?.status;
      const detail = e.response?.data?.detail || 'Failed to load buildings';
      set({ buildingsLoading: false, buildingsError: typeof detail === 'string' ? detail : 'Failed to load buildings' });
      get()._handleFetchError(e);
    }
  },

  fetchQueue: async () => {
    try {
      const res = await api.get('/buildings/queue');
      // API returns { village_id, queue: [...] } or possibly a plain array
      const arr = Array.isArray(res.data) ? res.data
        : Array.isArray(res.data?.queue) ? res.data.queue
        : [];
      set({ constructionQueue: arr });
    } catch (e) {
      console.warn('fetchQueue failed:', e);
      get()._handleFetchError(e);
    }
  },
}));

export default useGameStore;
