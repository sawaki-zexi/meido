import { defineConfig } from "vite";

export default defineConfig({
  root: "web/landing",
  publicDir: "../public",
  build: { outDir: "../dist-landing", emptyOutDir: true },
});
