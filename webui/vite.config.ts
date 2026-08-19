import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  build: {
    // Built assets are served by FastAPI from salmon/webui/static
    outDir: '../src/salmon/webui/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:55155', ws: true },
    },
  },
})
