import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The station serves the built files from src/web_dist; stations never need Node.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/web_dist",
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0, // Everything as files: the console's CSP allows no inline content.
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", ws: true },
    },
  },
});
