/**
 * The console's public origins (AP-03), validated at build time by next.config.ts and
 * inlined into the bundle under their own PUBLIC_* names. Nothing secret is public:
 * SESSION_FORWARD_SECRET is server-only and read by no FE-1 code path (FE-2 wires it).
 */
export interface PublicEnv {
  environment: "development" | "preview" | "production";
  apiOrigin: string;
  appOrigin: string;
  consoleOrigin: string;
  marketingOrigin: string;
}

export function publicEnv(): PublicEnv {
  const environment = process.env.PUBLIC_WASLA_ENV;
  const apiOrigin = process.env.PUBLIC_API_ORIGIN;
  const appOrigin = process.env.PUBLIC_APP_ORIGIN;
  const consoleOrigin = process.env.PUBLIC_CONSOLE_ORIGIN;
  const marketingOrigin = process.env.PUBLIC_MARKETING_ORIGIN;
  if (
    (environment !== "development" && environment !== "preview" && environment !== "production") ||
    !apiOrigin ||
    !appOrigin ||
    !consoleOrigin ||
    !marketingOrigin
  ) {
    throw new Error("the public origins were not set at build time (next.config.ts)");
  }
  return { environment, apiOrigin, appOrigin, consoleOrigin, marketingOrigin };
}
