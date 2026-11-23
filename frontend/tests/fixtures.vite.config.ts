import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

// Only the browser-test image builds these entries. Production Vite configuration
// keeps its normal index.html input and never copies this output into nginx.
export default defineConfig({
  root: fileURLToPath(new URL('../', import.meta.url)),
  base: '/__trading-fixture/',
  publicDir: false,
  build: {
    target: 'es2022', sourcemap: false, outDir: 'test-fixtures', emptyOutDir: true,
    rollupOptions: { input: {
      chart: fileURLToPath(new URL('fixtures/trading-chart-host.html', import.meta.url)),
      widgets: fileURLToPath(new URL('fixtures/trading-widgets-host.html', import.meta.url)),
    } },
  },
});
