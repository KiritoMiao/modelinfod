import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Served by the backend under /modelinfod (behind nginx/cloudflared).
export default defineConfig({
  base: '/modelinfod/',
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/modelinfod/api': 'http://localhost:8080',
    },
  },
})
