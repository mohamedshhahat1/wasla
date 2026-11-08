import { defineConfig } from "vitest/config";

export default defineConfig({
  // tsconfig keeps JSX for Next.js ("preserve"); the tests compile it with React's automatic runtime.
  oxc: { jsx: { runtime: "automatic" } },
  test: {
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
