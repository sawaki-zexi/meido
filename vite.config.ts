import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  server: { port: Number(process.env.MEIDO_WEB_PORT ?? 5288), strictPort: true, proxy: { "/api": process.env.MEIDO_API_ORIGIN ?? "http://127.0.0.1:4288" } },
  build: { outDir: "dist", emptyOutDir: true },
});
