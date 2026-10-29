import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// Served same-origin by the gateway at /ui/; the old interface stays at /ui/legacy/.
export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "../app-dist",
    emptyOutDir: true,
    assetsDir: "assets",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (/katex|rehype|remark|micromark|mdast|hast|unified|highlight\.js|react-markdown/.test(id)) return "markdown";
            if (/@radix-ui|cmdk|sonner/.test(id)) return "ui";
            if (/react-router|@tanstack|react-dom|scheduler|\/react\//.test(id)) return "react";
          }
        },
      },
    },
  },
  server: { proxy: { "/v1": "http://127.0.0.1:8800" } },
});
