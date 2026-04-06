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
    } catch {
      set({ connected: false, statusChecked: true });
    }
  },

  switchVillage: async (villageId) => {
    await api.post('/villages/switch', { village_id: villageId });
    set({ activeVillageId: villageId });
    await Promise.all([get().fetchResources(), get().fetchBuildings()]);
  },

  fetchResources: async () => {
    try {
      const res = await api.get('/buildings/resources');
      if (res.data && typeof res.data === 'object' && !Array.isArray(res.data)) {
        set({ resources: res.data });
      }
    } catch (e) { console.warn('Store fetch failed:', e) }
  },

  fetchBuildings: async () => {
    try {
      const res = await api.get('/buildings');
      set({ buildings: Array.isArray(res.data) ? res.data : [] });
    } catch (e) { console.warn('Store fetch failed:', e) }
  },

  fetchQueue: async () => {
    try {
      const res = await api.get('/buildings/queue');
      set({ constructionQueue: Array.isArray(res.data) ? res.data : [] });
    } catch (e) { console.warn('Store fetch failed:', e) }
  },
}));

export default useGameStore;
