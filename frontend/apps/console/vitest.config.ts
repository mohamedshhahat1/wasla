import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // FX-06 / FEF-08: worker threads, at most two per package - turbo runs every package's tests
    // beside the builds, and on a cold start forked workers missed vitest's start timeout.
    pool: "threads",
    maxWorkers: 2,
    include: ["test/**/*.test.{ts,tsx}"],
    environment: "node",
    testTimeout: 30_000,
    env: {
      PUBLIC_WASLA_ENV: "development",
      PUBLIC_API_ORIGIN: "http://localhost:8000",
      PUBLIC_APP_ORIGIN: "http://localhost:3000",
      PUBLIC_CONSOLE_ORIGIN: "http://localhost:3001",
      PUBLIC_MARKETING_ORIGIN: "http://localhost:4321",
    },
  },
});
