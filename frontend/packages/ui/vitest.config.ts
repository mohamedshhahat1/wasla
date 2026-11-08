import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // FX-06 / FEF-08: worker threads, at most two per package - turbo runs every package's tests
    // beside the builds, and on a cold start forked workers missed vitest's start timeout.
    pool: "threads",
    maxWorkers: 2,
    include: ["test/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["./test/setup.ts"],
    testTimeout: 30_000,
  },
});
