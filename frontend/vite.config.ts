import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /v1 and /healthz are proxied to the FastAPI server so the browser
// talks to one origin. In production VITE_API_URL points at API Gateway.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target: process.env.API_PROXY ?? "http://127.0.0.1:8000", changeOrigin: true },
      "/healthz": { target: process.env.API_PROXY ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  test: { environment: "node" },
});
