import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  timeout: 60000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5173",
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "../.venv/bin/python tests/fixture.py --port 3339",
      url: "http://127.0.0.1:3339/test/health",
      reuseExistingServer: false,
    },
    {
      command: process.env.WALLET_E2E_PRODUCTION
        ? "npm run preview -- --port 5173"
        : "npm run dev",
      url: "http://127.0.0.1:5173",
      reuseExistingServer: false,
    },
  ],
});
