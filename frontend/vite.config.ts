import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite picks up VITE_* env vars automatically (including from .env.local).
// VITE_API_BASE is read inside Workbench.jsx via import.meta.env.
//
// Dev server uses port 5173 (Vite default). Prod build emits to dist/.

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
