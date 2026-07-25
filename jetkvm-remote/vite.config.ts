import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base: './' makes the build work when loaded from file:// (Electron) and from
// the Capacitor WebView, not just a web server root.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
  },
  server: {
    host: true,
    port: 5173,
  },
});
