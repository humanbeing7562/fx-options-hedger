// Vite settings.
// While developing (`npm run dev`, page on port 5173), API calls are passed on to the
// FastAPI server on port 8000, so the page can use the same paths as in production.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/sessions": API,
      "/fixings": API,
      "/health": API,
    },
  },
});