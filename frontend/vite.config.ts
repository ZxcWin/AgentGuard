import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { port: 5173, strictPort: true, proxy: Object.fromEntries(
    ['/api', '/docs', '/openapi.json', '/v1', '/proxy'].map(path => [path, { target: 'http://127.0.0.1:8000', changeOrigin: true }]),
  ) },
})
