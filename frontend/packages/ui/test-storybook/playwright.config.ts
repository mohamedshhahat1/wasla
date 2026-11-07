import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright over the BUILT Storybook (DS-06, DS-07, DS-08): axe on every story in en and ar,
 * the RTL keyboard proof in three browsers, and (only when WASLA_SCREENSHOTS_DIR is set) the
 * screenshot contact sheet - evidence, never a baseline (FEQ-11).
 */
const PORT = 6007;

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: true,
  forbidOnly: Boolean(process.env["CI"]),
  retries: 0,
  workers: process.env["CI"] ? 2 : 4,
  reporter: [["list"], ["json", { outputFile: "../test-results/storybook-results.json" }]],
  outputDir: "../test-results/storybook",
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  webServer: {
    command: `node test-storybook/serve.mjs storybook-static ${PORT}`,
    cwd: "..",
    url: `http://127.0.0.1:${PORT}/index.json`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"], viewport: { width: 1280, height: 800 } },
      testMatch: /rtl-keyboard\.spec\.ts/,
    },
    {
      name: "webkit",
      use: { ...devices["Desktop Safari"], viewport: { width: 1280, height: 800 } },
      testMatch: /rtl-keyboard\.spec\.ts/,
    },
  ],
});
