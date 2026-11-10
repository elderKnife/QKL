import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: env.VITE_API_TARGET || 'http://127.0.0.1:8400', changeOrigin: true,
        configure: proxy => {
          const emitter = proxy as unknown as { on(event: string, listener: (request: { removeHeader(name: string): void }) => void): void }
          emitter.on('proxyReq', request => request.removeHeader('origin'))
        }
      }
    }
  },
  build: { chunkSizeWarningLimit: 1200 }
  }
})
