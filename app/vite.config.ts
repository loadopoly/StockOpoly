import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// StockOpoly UI dev server. Ports are deliberately clear of the existing
// orchestra (OCR app 3000, SCB receiver 8787, Streamlit 8501): the engine
// API runs on 8181 and this dev server on 3001, proxying /api and /intake
// through so the browser talks to one origin.
export default defineConfig({
  base: process.env.VITE_BASE ?? '/',
  plugins: [react()],
  resolve: {
    alias: { '@': resolve(__dirname, './src') },
  },
  server: {
    port: 3001,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8181', changeOrigin: true },
      '/intake': { target: 'http://127.0.0.1:8181', changeOrigin: true },
    },
  },
  build: {
    target: 'es2020',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('three') || id.includes('@react-three')) return 'vendor-3d';
          if (id.includes('react')) return 'vendor-react';
          if (id.includes('lucide-react')) return 'vendor-icons';
        },
      },
    },
  },
});
