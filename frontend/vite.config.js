import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api so the browser sees a single origin, which
// keeps CORS out of the picture and lets the SSE stream share it.
const target = process.env.VITE_API_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
        // SSE must stream rather than accumulate in a buffer.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },
})
