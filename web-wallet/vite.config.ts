import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  plugins: [
    react(),
    {
      name: "wallet-csp",
      transformIndexHtml: () =>
        command === "build"
          ? [
              {
                tag: "meta",
                attrs: {
                  "http-equiv": "Content-Security-Policy",
                  content:
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self' http: https:; object-src 'none'; base-uri 'self'; form-action 'none'",
                },
                injectTo: "head" as const,
              },
            ]
          : [],
    },
  ],
  server: { port: 5173, strictPort: true },
  test: { include: ["tests/**/*.test.ts"], testTimeout: 30000 },
}));
