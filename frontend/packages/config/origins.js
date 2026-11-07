/**
 * The four public origins per environment (AP-03, FEQ-04 answered 2026-11-07: iwasla.tech).
 * Read at BUILD time by each app's env module. Environment variables override the table; the
 * preview app origins have no table value (the *.workers.dev names belong to the owner's
 * Cloudflare account) and must be given, or the build fails.
 *
 * Production values are configuration only: FE-1 deploys nothing to iwasla.tech.
 */

export const ORIGIN_NAMES = /** @type {const} */ ([
  "PUBLIC_API_ORIGIN",
  "PUBLIC_APP_ORIGIN",
  "PUBLIC_CONSOLE_ORIGIN",
  "PUBLIC_MARKETING_ORIGIN",
]);

export const ENVIRONMENTS = /** @type {const} */ (["development", "preview", "production"]);

/** @type {Record<string, Partial<Record<string, string>>>} */
export const ORIGIN_TABLE = {
  development: {
    PUBLIC_API_ORIGIN: "http://localhost:8000",
    PUBLIC_APP_ORIGIN: "http://localhost:3000",
    PUBLIC_CONSOLE_ORIGIN: "http://localhost:3001",
    PUBLIC_MARKETING_ORIGIN: "http://localhost:4321",
  },
  // The e2e API is not reachable from Cloudflare; api.iwasla.tech does not answer yet and the
  // preview shells must not need it.
  preview: {
    PUBLIC_API_ORIGIN: "https://api.iwasla.tech",
  },
  production: {
    PUBLIC_API_ORIGIN: "https://api.iwasla.tech",
    PUBLIC_APP_ORIGIN: "https://app.iwasla.tech",
    PUBLIC_CONSOLE_ORIGIN: "https://console.iwasla.tech",
    PUBLIC_MARKETING_ORIGIN: "https://www.iwasla.tech",
  },
};

/**
 * @param {Record<string, string | undefined>} env  process.env (or import.meta.env)
 * @returns {{ environment: "development" | "preview" | "production", origins: Record<string, string> }}
 */
export function readOrigins(env) {
  const environment = env["WASLA_ENV"] ?? "";
  if (!ENVIRONMENTS.includes(/** @type {never} */ (environment))) {
    throw new Error(
      `WASLA_ENV must be one of ${ENVIRONMENTS.join(", ")} for a build (got ${JSON.stringify(environment)})`,
    );
  }
  /** @type {Record<string, string>} */
  const origins = {};
  const problems = [];
  for (const name of ORIGIN_NAMES) {
    const value = env[name] || ORIGIN_TABLE[environment]?.[name];
    if (!value) {
      problems.push(`${name} is missing for ${environment}`);
      continue;
    }
    let url;
    try {
      url = new URL(value);
    } catch {
      problems.push(`${name} is not a URL: ${value}`);
      continue;
    }
    if (url.origin !== value) {
      problems.push(`${name} must be an origin only (scheme://host[:port]), got ${value}`);
      continue;
    }
    const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
    if (environment === "development") {
      if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
        problems.push(`${name} must be https, or http on localhost in development: ${value}`);
      }
    } else if (url.protocol !== "https:") {
      problems.push(`${name} must be https outside development: ${value}`);
    }
    origins[name] = value;
  }
  if (problems.length) throw new Error(`Invalid origins:\n  ${problems.join("\n  ")}`);
  return {
    environment: /** @type {"development" | "preview" | "production"} */ (environment),
    origins,
  };
}
