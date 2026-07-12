import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    historyApiFallback: true,
    proxy: {
      '/api/copilotkit': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        // Fix: FastAPI redirects /api/copilotkit → /api/copilotkit/
        // Without this, the browser follows the redirect directly to 127.0.0.1:8001
        // bypassing the proxy → CORS error.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.statusCode && proxyRes.statusCode >= 300 && proxyRes.statusCode < 400) {
              const location = proxyRes.headers['location'];
              if (location && typeof location === 'string' && location.includes('127.0.0.1:8001')) {
                proxyRes.headers['location'] = location.replace('http://127.0.0.1:8001', '');
              }
            }
          });
        },
      },
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})
