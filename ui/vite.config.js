import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // In development the UI runs on Vite (port 5173) and the API on uvicorn
    // (port 8000); forwarding /api keeps every fetch same-origin, as it is
    // when the API serves the built UI.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.js"],
  },
});
