import { defineConfig } from 'vite';
export default defineConfig({ root: 'desktop-pet/renderer', base: './',
  build: { outDir: '../dist/renderer', emptyOutDir: true } });
