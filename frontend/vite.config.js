import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/travian_api/web/static',
    emptyOutDir: true,
    oxc: {
      drop: ['console', 'debugger'],
    },
  },
  server: {
    proxy: {
      // Default 8001 matches what travian-web and start.bat actually launch;
      // override with TRAVIAN_BACKEND_PORT (e.g. 8000 for the stable server).
      '/api': `http://localhost:${process.env.TRAVIAN_BACKEND_PORT || '8001'}`,
      '/ws': {
        target: `ws://localhost:${process.env.TRAVIAN_BACKEND_PORT || '8001'}`,
        ws: true,
      },
    },
  },
})
