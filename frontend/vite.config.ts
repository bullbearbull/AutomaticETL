import path from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/** The local AutoETL UI server (`autoetl ui`, default port 8765). */
const API_TARGET = 'http://127.0.0.1:8765';

const srcRoot = fileURLToPath(new URL('./src', import.meta.url));

/**
 * CSS Module class names: the file path relative to `src/` plus the local
 * class name, for example `components-Button-Button_root`. No hashes, and the
 * same on every OS, so repeat builds produce identical output.
 */
function scopedClassName(local: string, filename: string): string {
  const file = filename.split('?')[0] ?? filename;
  const relative = path
    .relative(srcRoot, file)
    .replace(/\\/g, '/')
    .replace(/\.module\.css$/, '');
  const prefix = relative.replace(/[^A-Za-z0-9_-]+/g, '-');
  return `${prefix}_${local}`;
}

export default defineConfig({
  base: '/next/',
  plugins: [react()],
  css: {
    modules: { generateScopedName: scopedClassName },
  },
  server: {
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        // The server rejects a POST whose Origin is not `http://<Host>`.
        // changeOrigin rewrites Host only, so rewrite Origin to match it.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            if (proxyReq.getHeader('origin') !== undefined) {
              proxyReq.setHeader('Origin', API_TARGET);
            }
          });
        },
      },
    },
  },
  build: {
    outDir: '../src/autoetl/ui/static',
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
    modulePreload: { polyfill: false },
    license: { fileName: 'THIRD_PARTY_LICENSES.md' },
    rolldownOptions: {
      output: {
        // Tab files import shared code from index.js by its real export
        // name (not a minified letter), so one tab starting to use another
        // shared export does not rename the imports in the other tabs.
        minifyInternalExports: false,
        // Fixed names, no hashes: index.js/index.css hold the shared code,
        // and each tab's lazy entry (features/<tab>/<Name>Tab.tsx) becomes
        // assets/<Name>Tab.js and assets/<Name>Tab.css.
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
