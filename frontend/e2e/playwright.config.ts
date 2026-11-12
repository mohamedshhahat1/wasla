import { defineConfig, devices } from "@playwright/test";

import { APPS, E2E_FORWARD_SECRET } from "./support/env";

/**
 * The FE-1 e2e harness (TE-02) against FE-0's backend profile, which `pnpm e2e:local` (or the
 * CI job) starts and stops - this config never touches Docker. The apps run from their
 * production builds (`next start`; the marketing site on wrangler's local Workers runtime).
 * Projects: three browsers × desktop 1280 and phone 390; locales are chosen per test.
 */
const telemetryOff = {
  NEXT_TELEMETRY_DISABLED: "1",
  WRANGLER_SEND_METRICS: "false",
  WASLA_ENV: "development",
};

const next = (app: "web" | "console", port: number, testRoutes: boolean) => ({
  command: `pnpm --dir ../apps/${app} exec next start -p ${port}`,
  url: `http://localhost:${port}/`,
  env: {
    ...telemetryOff,
    // FE-2: the session routes sign with the e2e stack's PUBLIC test secret (never a deployment's).
    SESSION_FORWARD_SECRET: E2E_FORWARD_SECRET,
    ...(testRoutes ? { WASLA_E2E_ROUTES: "1" } : {}),
  },
  reuseExistingServer: false,
  timeout: 120_000,
});

const desktop = { viewport: { width: 1280, height: 800 } };
const phone = { viewport: { width: 390, height: 844 }, isMobile: false, hasTouch: true };

const STREAM_RESTART = /fe2-stream\.spec\.ts$/;
// FE-RD (VQ-01, VQ-03, VQ-04): the colour-role and target proofs run in Chromium only, at 1280 and
// 390; FE-2's six projects never run them, so their counts are unchanged.
const VISUAL_ROLES = /visual-roles\.spec\.ts$/;

const BROWSERS = [
  { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], ...desktop } },
  { name: "chromium-phone", use: { ...devices["Desktop Chrome"], ...phone } },
  { name: "firefox-desktop", use: { ...devices["Desktop Firefox"], ...desktop } },
  { name: "firefox-phone", use: { ...devices["Desktop Firefox"], ...phone } },
  { name: "webkit-desktop", use: { ...devices["Desktop Safari"], ...desktop } },
  { name: "webkit-phone", use: { ...devices["Desktop Safari"], ...phone } },
];

export default defineConfig({
  testDir: "./tests",
  globalSetup: "./support/global-setup.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env["CI"]),
  retries: 0,
  workers: process.env["CI"] ? 3 : 4,
  timeout: 60_000,
  reporter: [
    ["list"],
    ["json", { outputFile: "test-results/e2e-results.json" }],
    ["html", { open: "never" }],
  ],
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    next("web", 3000, true),
    next("console", 3001, true),
    next("web", 3002, false),
    next("console", 3003, false),
    {
      command:
        "pnpm --dir ../apps/marketing exec wrangler dev --port 4321 --local --log-level warn",
      url: `${APPS.marketing.origin}/en/`,
      env: telemetryOff,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
  projects: [
    ...BROWSERS.map(({ name, use }) => ({ name, use, testIgnore: [STREAM_RESTART, VISUAL_ROLES] })),
    {
      name: "visual-roles-desktop",
      use: { ...devices["Desktop Chrome"], ...desktop },
      testMatch: VISUAL_ROLES,
    },
    {
      name: "visual-roles-phone",
      use: { ...devices["Desktop Chrome"], ...phone },
      testMatch: VISUAL_ROLES,
    },
    // FX-07's test restarts the API, so it runs alone after every other project.
    {
      name: "stream-restart",
      use: { ...devices["Desktop Chrome"], ...desktop },
      testMatch: STREAM_RESTART,
      dependencies: BROWSERS.map(({ name }) => name),
    },
  ],
});
