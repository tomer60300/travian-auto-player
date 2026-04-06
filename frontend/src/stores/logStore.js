import { create } from 'zustand'

let _id = 0
const MAX_ENTRIES = 2000

const useLogStore = create((set) => ({
  entries: [],

  addLog: (level, source, message, detail) => {
    const entry = {
      id: ++_id,
      timestamp: Date.now(),
      level,   // 'info' | 'success' | 'warning' | 'error'
      source,  // 'api' | 'ws' | 'auth' | 'game' | 'scout' | 'farm' | 'queue' | 'military' | 'video' | 'reports'
      message,
      detail,  // optional extra data (string or object)
    }
    set((state) => ({
      entries: [...state.entries.slice(-(MAX_ENTRIES - 1)), entry],
    }))
  },

  clear: () => set({ entries: [] }),
}))

export default useLogStore
