import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The built bundle is served by FastAPI from dashboard/dist at "/", so assets
// must be requested with relative paths — the app is mounted at the root but
// the mount is a StaticFiles mount, not a dev server.
//
// In `npm run dev` the API lives on a separate port, so every endpoint the
// dashboard reads is proxied through to the FastAPI process. That keeps the
// client code using the same relative URLs in both modes.
const API_TARGET = process.env.TOLLGATE_API ?? 'http://127.0.0.1:8000'

const apiPaths = [
  '/sessions',
  '/ledger',
  '/escalations',
  '/metrics',
  '/health',
]

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // One projector, one process: no code splitting to chase down.
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      apiPaths.map((p) => [p, { target: API_TARGET, changeOrigin: true }]),
    ),
  },
})
