/**
 * The customer app's public origins (AP-03), validated at build time by next.config.ts and
 * inlined into the bundle under their own PUBLIC_* names. Nothing secret is public:
 * SESSION_FORWARD_SECRET is server-only, read by the /session/* handlers alone (FE-2).
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

/**
 * FEF-26: true only in a preview build made without an API behind it (PUBLIC_PREVIEW_NO_API,
 * inlined by next.config.ts and set by the preview Workers build alone). A build-time fact - never
 * a probe, the hostname or a cookie. Such a build says it is a preview instead of "Connection lost".
 */
export function previewWithoutApi(): boolean {
  return process.env.PUBLIC_PREVIEW_NO_API === "true";
}

/**
 * Whether the Google button is shown (APP-AUTH-03): PUBLIC_GOOGLE_ENABLED at build time, false
 * unless set to "true" (FEB-16's workaround until the API publishes its auth configuration).
 */
export function googleEnabled(): boolean {
  return process.env.PUBLIC_GOOGLE_ENABLED === "true";
}
