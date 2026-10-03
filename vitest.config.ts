import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { root: "web", environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"], restoreMocks: true, setupFiles: ["src/test/setup.ts"] },
});
