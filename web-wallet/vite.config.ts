import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

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
                    "default-src 'self'; script-src 'self'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self' http: https:; object-src 'none'; base-uri 'self'; form-action 'none'",
                },
                injectTo: "head" as const,
              },
            ]
          : [],
    },
  ],
  server: { port: 5173, strictPort: true },
  build: {
    rollupOptions: {
      input: {
        landing: fileURLToPath(new URL("./index.html", import.meta.url)),
        cryptography: fileURLToPath(
          new URL("./how-it-works/index.html", import.meta.url),
        ),
        wallet: fileURLToPath(new URL("./wallet/index.html", import.meta.url)),
      },
    },
  },
  test: { include: ["tests/**/*.test.ts"], testTimeout: 30000 },
}));
