import { create } from 'zustand'

let _id = 0
const MAX_ENTRIES = 2000

const useLogStore = create((set) => ({
  entries: [],
  serverLogCount: 0,
  drawerOpen: false,
  drawerHeight: 300,

  toggleDrawer: () => set((state) => ({ drawerOpen: !state.drawerOpen })),
  setDrawerOpen: (open) => set({ drawerOpen: open }),
  setDrawerHeight: (h) => set({ drawerHeight: h }),

  addLog: (level, source, message, detail, origin = 'client') => {
    const entry = {
      id: ++_id,
      timestamp: Date.now(),
      level,   // 'info' | 'success' | 'warning' | 'error'
      source,  // 'api' | 'ws' | 'auth' | 'game' | 'scout' | 'farm' | 'queue' | 'military' | 'video' | 'reports' | 'server'
      message,
      detail,  // optional extra data (string or object)
      origin,  // 'client' | 'server'
    }
    set((state) => ({
      entries: [...state.entries.slice(-(MAX_ENTRIES - 1)), entry],
      serverLogCount: origin === 'server' ? state.serverLogCount + 1 : state.serverLogCount,
    }))
  },

  resetServerLogCount: () => set({ serverLogCount: 0 }),

  clear: () => set({ entries: [], serverLogCount: 0 }),
}))

export default useLogStore
