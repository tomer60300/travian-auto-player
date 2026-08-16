import { create } from 'zustand';
import api from '../api';

const VILLAGE_KEY = 'activeVillageId'

// Scoped per (server, player): village ids can overlap across worlds, so one
// global key could silently inherit another account's selection and route
// actions to the wrong village after switching accounts in the same tab.
function villageStorageKey(serverUrl, playerName) {
  return serverUrl && playerName ? `${VILLAGE_KEY}::${serverUrl}|${playerName}` : null
}

// Identity a fetch was issued under: village AND account. A village-only stamp
// is not enough because ids overlap across worlds, so an in-flight response
// from world A could pass a bare-vid check and overwrite world B's state.
function fetchStamp(state) {
  return `${state.activeVillageId}@${state.serverUrl}|${state.playerName}`
}

function getStoredVillageId(key) {
  if (!key) return null
  try {
    const v = sessionStorage.getItem(key)
    return v ? Number(v) : null
  } catch { return null }
}

function storeVillageId(key, id) {
  if (!key) return
  try {
    if (id != null) sessionStorage.setItem(key, String(id))
    else sessionStorage.removeItem(key)
  } catch { /* empty */ }
}

// NOTE: the tab-local selection is deliberately NOT pushed to the backend
// here. session.active_village_id is shared across every tab of the user, so
// a background sync from one tab would silently retarget another tab's
// default. Every call that acts on a village sends its village_id explicitly;
// the backend default only changes on an explicit user switch.

let _checkingStatus = false

const useGameStore = create((set, get) => ({
  connected: false,
  statusChecked: false,
  serverUrl: null,
  playerName: null,
  tribeId: null,
  villages: [],
  // Populated by connect/checkStatus once the account (and so the per-account
  // storage key) is known.
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
    const vkey = villageStorageKey(data.server_url, data.player_name)
    const storedVid = getStoredVillageId(vkey)
    const villages = Array.isArray(data.villages) ? data.villages : []
    const villageToUse = (storedVid && villages.some(v => v.id === storedVid))
      ? storedVid
      : data.active_village_id
    set({
      connected: true,
      statusChecked: true,
      serverUrl: data.server_url,
      playerName: data.player_name,
      tribeId: data.tribe_id,
      activeVillageId: villageToUse,
      villages: villages,
    });
    storeVillageId(vkey, villageToUse)
    return data;
  },

  connectFromSaved: async (serverId) => {
    const res = await api.post(`/travian/servers/${serverId}/connect`);
    const data = res.data;
    const vkey = villageStorageKey(data.server_url, data.player_name)
    const storedVid = getStoredVillageId(vkey)
    const villages = Array.isArray(data.villages) ? data.villages : []
    const villageToUse = (storedVid && villages.some(v => v.id === storedVid))
      ? storedVid
      : data.active_village_id
    set({
      connected: true,
      statusChecked: true,
      serverUrl: data.server_url,
      playerName: data.player_name,
      tribeId: data.tribe_id,
      activeVillageId: villageToUse,
      villages: villages,
    });
    storeVillageId(vkey, villageToUse)
    return data;
  },

  disconnect: async () => {
    // No catch: clearing local state on a failed DELETE would paint the UI
    // "disconnected" while the backend session stays alive and working (the
    // server also refuses with a 409 while operations run). The caller
    // surfaces the failure; state only changes when the backend confirmed.
    await api.delete('/travian/disconnect')
    storeVillageId(villageStorageKey(get().serverUrl, get().playerName), null)
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
        const vkey = villageStorageKey(data.server_url, data.player_name)
        const storedVid = getStoredVillageId(vkey)
        const villages = Array.isArray(data.villages) ? data.villages : []
        const villageToUse = (storedVid && villages.some(v => v.id === storedVid))
          ? storedVid
          : data.active_village_id
        set({
          connected: true,
          statusChecked: true,
          serverUrl: data.server_url,
          playerName: data.player_name,
          tribeId: data.tribe_id,
          activeVillageId: villageToUse,
          villages: villages,
        });
        storeVillageId(vkey, villageToUse)
      } else {
        // Clear account-scoped state too: pages that key off serverUrl or
        // playerName (Resource Planner) must not keep showing — or persisting
        // under — the previous account after the session expired.
        set({
          connected: false, statusChecked: true, serverUrl: null,
          playerName: null, tribeId: null, villages: [], activeVillageId: null,
          resources: null, buildings: [], constructionQueue: [],
        });
      }
    } catch (e) { console.warn('Store fetch failed:', e)
      // A transport failure is NOT a confirmed logout — a real logout returns
      // 200 with connected:false (cleared in the else branch above). Leave
      // account state intact so a brief 5xx/network blip does not reset
      // account-scoped pages; the next poll (or a 403 from another call)
      // reconciles. Only mark the first check done so the app can render.
      set({ statusChecked: true });
    }
  },

  switchVillage: async (villageId) => {
    await api.post('/villages/switch', { village_id: villageId });
    set({ activeVillageId: villageId });
    storeVillageId(villageStorageKey(get().serverUrl, get().playerName), villageId)
    await Promise.all([get().fetchResources(), get().fetchBuildings(), get().fetchQueue()]);
  },

  // Helper: if a 403 means "not connected to Travian", verify with checkStatus
  _handleFetchError: (e) => {
    const status = e.response?.status;
    if (status === 403) {
      const current = useGameStore.getState();
      if (current.connected && !_checkingStatus) {
        _checkingStatus = true
        get().checkStatus().finally(() => { _checkingStatus = false })
      }
    }
  },

  fetchResources: async () => {
    // Never send a village-less read: the backend would resolve it to the
    // account's default village, which may not be the one the user is on.
    // Fewer/again-correct Travian requests beats a guess that needs redoing.
    const vid = get().activeVillageId
    if (!vid) return
    const stamp = fetchStamp(get())
    try {
      const res = await api.get('/buildings/resources', { params: { village_id: vid } });
      // Drop a response the user has since switched away from — a different
      // village OR a different account (village ids overlap across worlds):
      // otherwise A's resources land under B's selector.
      if (fetchStamp(get()) !== stamp) return;
      if (res.data && typeof res.data === 'object' && !Array.isArray(res.data)) {
        set({ resources: res.data });
      }
    } catch (e) {
      console.warn('fetchResources failed:', e);
      get()._handleFetchError(e);
    }
  },

  fetchBuildings: async () => {
    const vid = get().activeVillageId
    if (!vid) return
    const stamp = fetchStamp(get())
    set({ buildingsLoading: true, buildingsError: null });
    try {
      const res = await api.get('/buildings', { params: { village_id: vid } });
      // A newer switch (village or account) owns the store now; its own fetch
      // will settle loading.
      if (fetchStamp(get()) !== stamp) return;
      // API returns { village_id, buildings: [...] } or possibly a plain array
      const arr = Array.isArray(res.data) ? res.data
        : Array.isArray(res.data?.buildings) ? res.data.buildings
        : [];
      set({ buildings: arr, buildingsLoading: false });
    } catch (e) {
      console.warn('fetchBuildings failed:', e);
      const detail = e.response?.data?.detail || 'Failed to load buildings';
      set({ buildingsLoading: false, buildingsError: typeof detail === 'string' ? detail : 'Failed to load buildings' });
      get()._handleFetchError(e);
    }
  },

  fetchQueue: async () => {
    const vid = get().activeVillageId
    if (!vid) return
    const stamp = fetchStamp(get())
    try {
      const res = await api.get('/buildings/queue', { params: { village_id: vid } });
      if (fetchStamp(get()) !== stamp) return;
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

  refreshVillageData: async () => {
    await Promise.all([get().fetchResources(), get().fetchBuildings(), get().fetchQueue()]);
  },
}));

export default useGameStore;
