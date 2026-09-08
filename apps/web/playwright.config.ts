import { defineConfig, devices } from "@playwright/test";

// Runs against an already-running stack (`docker compose up`, or `pnpm dev` + the API locally) —
// no webServer here, since a real signup/login round trip needs the actual API, Postgres, and
// Redis behind it, not just `next dev` on its own. See README.md's Testing section for how to
// run this.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
