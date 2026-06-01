import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  // VITE_API_TARGET sets where the dev-server proxy forwards /api/* requests.
  // Default: localhost:8000 (backend on the same machine).
  // Override in .env when the backend runs on a different host, e.g.:
  //   VITE_API_TARGET=http://192.168.1.50:8000
  const env = loadEnv(mode, process.cwd(), '');
  const apiTarget = env.VITE_API_TARGET || 'http://localhost:8000';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target:      apiTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      rolldownOptions: {
        output: {
          manualChunks: {
            reactflow: ['reactflow'],
            vendor:    ['react', 'react-dom', 'axios', 'zustand'],
          },
        },
      },
    },
  };
});
