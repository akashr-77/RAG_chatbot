import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Forward /api/* requests to FastAPI — avoids CORS in dev
      '/api': 'http://127.0.0.1:8001',
    },
  },
})