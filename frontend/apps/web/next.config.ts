import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

import { readOrigins } from "@wasla/config/origins";

// Fails the build on a missing or non-https origin (AP-03).
const { environment, origins } = readOrigins(process.env);

const config: NextConfig = {
  poweredByHeader: false,
  // FX-08: next dev must not write AGENTS.md into the tree when it detects a coding agent.
  agentRules: false,
  reactStrictMode: true,
  // Image optimisation off: several 2026 advisories were in that API (plan section 22).
  images: { unoptimized: true },
  transpilePackages: ["@wasla/ui", "@wasla/i18n", "@wasla/api-client", "@wasla/session"],
  env: {
    PUBLIC_WASLA_ENV: environment,
    PUBLIC_API_ORIGIN: origins["PUBLIC_API_ORIGIN"],
    PUBLIC_APP_ORIGIN: origins["PUBLIC_APP_ORIGIN"],
    PUBLIC_CONSOLE_ORIGIN: origins["PUBLIC_CONSOLE_ORIGIN"],
    PUBLIC_MARKETING_ORIGIN: origins["PUBLIC_MARKETING_ORIGIN"],
    // FEB-16's workaround: the Google button exists only in builds that say so (APP-AUTH-03).
    PUBLIC_GOOGLE_ENABLED: process.env["PUBLIC_GOOGLE_ENABLED"] === "true" ? "true" : "false",
  },
};

export default createNextIntlPlugin("./i18n/request.ts")(config);
