import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Dev: proxy API to FastAPI so the session cookie is same-origin and there's no CORS.
const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8100"; // compose publishes the api on 8100

export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: false, css: false },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/healthz": { target: BACKEND, changeOrigin: true },
    },
  },
});
