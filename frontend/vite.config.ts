import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: {
    proxy: {
      "/api/v1": {
        target: process.env.HL_APP_DEV_PROXY || "http://127.0.0.1:8000",
      },
    },
  },
});
