import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

const here = path.dirname(fileURLToPath(import.meta.url));
const ORT_FILES = [
  'ort-wasm-simd-threaded.wasm',
  'ort-wasm-simd-threaded.mjs',
  'ort-wasm-simd-threaded.jsep.wasm',
  'ort-wasm-simd-threaded.jsep.mjs',
];

/**
 * Serve ONNX Runtime Web's wasm assets locally so inference does not rely on a
 * third-party CDN. Files are copied from node_modules, never committed.
 */
function localOrtAssets(): Plugin {
  return {
    name: 'local-ort-assets',
    buildStart() {
      const src = path.resolve(here, '..', 'node_modules', 'onnxruntime-web', 'dist');
      const dest = path.resolve(here, 'public', 'ort');
      if (!fs.existsSync(src)) return;
      fs.mkdirSync(dest, { recursive: true });
      for (const file of ORT_FILES) {
        const from = path.join(src, file);
        if (fs.existsSync(from)) fs.copyFileSync(from, path.join(dest, file));
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), localOrtAssets()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true,
      },
    },
  },
  worker: {
    format: 'es',
  },
  build: {
    target: 'es2022',
  },
});
