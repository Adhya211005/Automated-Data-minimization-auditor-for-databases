import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: proxy /api/* to the FastAPI backend (uvicorn auditor.api.main:app on :8000)
// so the browser makes same-origin requests and we don't need CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
