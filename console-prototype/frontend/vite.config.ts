import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18082', changeOrigin: true,
        configure: proxy => {
          const emitter = proxy as unknown as { on(event: string, listener: (request: { removeHeader(name: string): void }) => void): void }
          emitter.on('proxyReq', request => request.removeHeader('origin'))
        }
      }
    }
  },
  build: { chunkSizeWarningLimit: 1200 }
})
