import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import path from "path"

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Retain source maps for Doctor's source_map_resolve tool (see §13.1.2).
  build: {
    sourcemap: true,
  },
  server: {
    // SPA fallback — redirect all non-file requests to index.html
    // so React Router can handle client-side routes like /tasks/:id.
    historyApiFallback: true,
    proxy: {
      // Forward ALL /api/* requests to the backend in dev mode.
      // (In Docker, nginx handles this; in dev, Vite must proxy.)
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
})
