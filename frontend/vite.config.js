import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/agui": "http://localhost:8000",
      "/panel": "http://localhost:8000",
      "/news": "http://localhost:8000",
      "/stats": "http://localhost:8000",
    },
  },
});
