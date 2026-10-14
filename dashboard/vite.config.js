import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API and the MCP endpoint to a gateway on :8000, the same paths Traefik routes to the
// gateway in the cluster. So the app always calls same-origin /api: no CORS, one cookie.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/mcp": "http://localhost:8000",
    },
  },
});
