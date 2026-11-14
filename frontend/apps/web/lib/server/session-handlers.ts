/**
 * The eight S-2 route handlers' logic (FE-2, SR-01..SR-09, ADR-193). SERVER-ONLY: imported by
 * app/session/** route files alone. These are the only places a refresh token exists in the
 * frontend tier: read from the HttpOnly cookie, sent to the API, written back as a cookie -
 * never into a response body.
 *
 * Each handler: the request checks (no API call before they pass), then the forward secret and
 * the client address, then one or two signed API calls, then the cookie.
 */
import {
  callApi,
  checkSessionRequest,
  clearBindingCookieHeaders,
  clearRefreshCookieHeader,
  clientIpFrom,
  DEFAULT_REFRESH_LIFETIME_SECONDS,
  envelopeResponse,
  passThroughError,
  presentedBinding,
  readCookie,
  readSessionBody,
  refreshClearsCookie,
  refreshCookieHeader,
  refreshCookieName,
  relayBindingCookie,
  sessionResponseHeaders,
  UpstreamTimeout,
  type ApiAnswer,
  type WaslaEnvironment,
} from "@wasla/session/server";

export const SESSION_VERBS = [
  "login",
  "register-login",
  "refresh",
  "switch",
  "logout",
  "logout-all",
  "google/authorize",
  "google/callback",
] as const;
export type SessionVerb = (typeof SESSION_VERBS)[number];

export interface SessionServerConfig {
  environment: WaslaEnvironment;
  appOrigin: string;
  apiOrigin: string;
  /** SESSION_FORWARD_SECRET; null when the environment does not set it (→ 503). */
  secret: string | null;
  /** WASLA_E2E_ROUTES=1 on a development build only. */
  e2eRoutes: boolean;
  fetch?: typeof globalThis.fetch;
  /** The API call timeout (API_TIMEOUT_MS, 10 s, unless a test injects a shorter one). */
  apiTimeoutMs?: number;
  now?: () => number;
  /** Where the one "secret missing" line goes (never the value). */
  log?: (line: string) => void;
}

/** The handlers' configuration from the build-time origins and the server's environment. */
export function sessionConfigFromEnv(): SessionServerConfig {
  const environment = process.env["PUBLIC_WASLA_ENV"];
  const appOrigin = process.env["PUBLIC_APP_ORIGIN"];
  const apiOrigin = process.env["PUBLIC_API_ORIGIN"];
  if (
    (environment !== "development" && environment !== "preview" && environment !== "production") ||
    !appOrigin ||
    !apiOrigin
  ) {
    throw new Error("the public origins were not set at build time (next.config.ts)");
  }
  // Fails a non-development start whose cookie would lack the __Host- prefix (SR-03).
  refreshCookieName(environment);
  return {
    environment,
    appOrigin,
    apiOrigin,
    secret: process.env["SESSION_FORWARD_SECRET"] || null,
    e2eRoutes: environment === "development" && process.env["WASLA_E2E_ROUTES"] === "1",
  };
}

interface Context {
  config: SessionServerConfig;
  request: Request;
  body: Record<string, unknown>;
  clientIp: string;
  secret: string;
  api(
    path: string,
    options?: { body?: Record<string, unknown>; bearer?: string | null; cookie?: string | null },
  ): Promise<ApiAnswer>;
}

const AUTH = "/api/v1/auth";

function str(value: unknown, max = 4096): string | undefined {
  return typeof value === "string" && value.length <= max ? value : undefined;
}

/** Only the fields the API takes, so nothing else a page sends reaches it. */
function pick(body: Record<string, unknown>, fields: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    const value = body[field];
    if (typeof value === "string" || value === null) out[field] = value;
  }
  return out;
}

function bearerOf(request: Request): string | null {
  const header = request.headers.get("Authorization") ?? "";
  const match = /^Bearer ([A-Za-z0-9._~+/=-]{1,4096})$/.exec(header);
  return match?.[1] ?? null;
}

/** The API envelope's code, or null for a body without one. */
function apiErrorCode(answer: ApiAnswer): string | null {
  const envelope =
    typeof answer.json === "object" && answer.json !== null
      ? (answer.json as { error?: { code?: unknown } }).error
      : undefined;
  return typeof envelope?.code === "string" ? envelope.code : null;
}

function json(status: number, body: unknown, headers: Headers): Response {
  return new Response(JSON.stringify(body), { status, headers });
}

interface SessionAnswer {
  access_token?: unknown;
  refresh_token?: unknown;
  expires_in?: unknown;
  active_workspace?: unknown;
}

/** A 2xx session answer: the cookie set, the body WITHOUT the refresh token (SR-03). */
function sessionEstablished(ctx: Context, answer: ApiAnswer, extra: string[] = []): Response {
  const data = (answer.json ?? {}) as SessionAnswer;
  if (
    typeof data.access_token !== "string" ||
    typeof data.refresh_token !== "string" ||
    typeof data.expires_in !== "number"
  ) {
    return envelopeResponse(502, "unexpected_response", answer.requestId);
  }
  const headers = sessionResponseHeaders({ "X-Request-ID": answer.requestId });
  headers.append(
    "Set-Cookie",
    refreshCookieHeader(
      ctx.config.environment,
      data.refresh_token,
      DEFAULT_REFRESH_LIFETIME_SECONDS,
    ),
  );
  for (const cookie of extra) headers.append("Set-Cookie", cookie);
  return json(
    200,
    {
      access_token: data.access_token,
      expires_in: data.expires_in,
      active_workspace: data.active_workspace ?? null,
    },
    headers,
  );
}

function withCleared(response: Response, cookies: string[]): Response {
  for (const cookie of cookies) response.headers.append("Set-Cookie", cookie);
  return response;
}

const HANDLERS: Record<SessionVerb, (ctx: Context) => Promise<Response>> = {
  async login(ctx) {
    const answer = await ctx.api(`${AUTH}/login`, {
      body: pick(ctx.body, ["email", "password", "workspace_slug"]),
    });
    return answer.status === 200 ? sessionEstablished(ctx, answer) : passThroughError(answer);
  },

  async "register-login"(ctx) {
    const registered = await ctx.api(`${AUTH}/register`, {
      body: pick(ctx.body, [
        "email",
        "password",
        "full_name",
        "workspace_name",
        "workspace_slug",
        "timezone",
      ]),
    });
    if (registered.status !== 202) return passThroughError(registered);
    const login = await ctx.api(`${AUTH}/login`, { body: pick(ctx.body, ["email", "password"]) });
    if (login.status === 200) return sessionEstablished(ctx, login);
    if (login.status === 401) {
      // The address was taken (the API answered 202 regardless): no enumeration, no cookie.
      const headers = sessionResponseHeaders({ "X-Request-ID": login.requestId });
      return json(200, { status: "check_inbox" }, headers);
    }
    return passThroughError(login);
  },

  async refresh(ctx) {
    const cookieName = refreshCookieName(ctx.config.environment);
    const token = readCookie(ctx.request, cookieName);
    if (!token) return envelopeResponse(401, "unauthenticated", null);
    const slug = str(ctx.body["workspace_slug"], 100);
    const answer = await ctx.api(`${AUTH}/refresh`, {
      body: { refresh_token: token, ...(slug ? { workspace_slug: slug } : {}) },
    });
    if (answer.status === 200) return sessionEstablished(ctx, answer);
    // FEF-24: a suspended workspace refuses with its code and keeps the person signed in - the
    // cookie stays (the API did not spend it); every other refusal clears it, as before.
    if (refreshClearsCookie(answer.status, apiErrorCode(answer))) {
      return withCleared(passThroughError(answer), [
        clearRefreshCookieHeader(ctx.config.environment),
      ]);
    }
    return passThroughError(answer);
  },

  async switch(ctx) {
    const slug = str(ctx.body["workspace_slug"], 100);
    if (!slug) return envelopeResponse(422, "validation_error", null);
    const answer = await ctx.api(`${AUTH}/workspace`, {
      body: { workspace_slug: slug },
      bearer: bearerOf(ctx.request),
    });
    if (answer.status !== 200) return passThroughError(answer);
    const data = (answer.json ?? {}) as SessionAnswer;
    if (typeof data.access_token !== "string" || typeof data.expires_in !== "number") {
      return envelopeResponse(502, "unexpected_response", answer.requestId);
    }
    // POST /auth/workspace returns no refresh token: the cookie is left as it is.
    return json(
      200,
      {
        access_token: data.access_token,
        expires_in: data.expires_in,
        active_workspace: data.active_workspace ?? null,
      },
      sessionResponseHeaders({ "X-Request-ID": answer.requestId }),
    );
  },

  async logout(ctx) {
    const token = readCookie(ctx.request, refreshCookieName(ctx.config.environment));
    let requestId: string | null = null;
    if (token) {
      const answer = await ctx.api(`${AUTH}/logout`, { body: { refresh_token: token } });
      requestId = answer.requestId;
    }
    // Cleared whatever the API said: a token it no longer knows still leaves this browser.
    const headers = sessionResponseHeaders(requestId ? { "X-Request-ID": requestId } : {});
    headers.append("Set-Cookie", clearRefreshCookieHeader(ctx.config.environment));
    return json(200, {}, headers);
  },

  async "logout-all"(ctx) {
    const answer = await ctx.api(`${AUTH}/logout-all`, { bearer: bearerOf(ctx.request) });
    if (answer.status !== 200) return passThroughError(answer);
    const headers = sessionResponseHeaders({ "X-Request-ID": answer.requestId });
    headers.append("Set-Cookie", clearRefreshCookieHeader(ctx.config.environment));
    return json(200, {}, headers);
  },

  async "google/authorize"(ctx) {
    const answer = await ctx.api(`${AUTH}/google/authorize`);
    if (answer.status !== 200) return passThroughError(answer);
    const url = (answer.json as { authorization_url?: unknown } | null)?.authorization_url;
    const binding = relayBindingCookie(answer.headers.getSetCookie());
    if (typeof url !== "string" || !/^https?:\/\//.test(url) || !binding) {
      return envelopeResponse(502, "unexpected_response", answer.requestId);
    }
    const headers = sessionResponseHeaders({ "X-Request-ID": answer.requestId });
    headers.append("Set-Cookie", binding.setCookie);
    return json(200, { authorization_url: url }, headers);
  },

  async "google/callback"(ctx) {
    const cleared = clearBindingCookieHeaders();
    const code = str(ctx.body["code"], 2048);
    const state = str(ctx.body["state"], 128);
    if (!code || !state) {
      return withCleared(envelopeResponse(422, "validation_error", null), cleared);
    }
    const binding = presentedBinding(ctx.request);
    const answer = await ctx.api(`${AUTH}/google/callback`, {
      body: { code, state },
      cookie: binding ? `${binding.name}=${binding.value}` : null,
    });
    if (answer.status === 200) return sessionEstablished(ctx, answer, cleared);
    return withCleared(passThroughError(answer), cleared);
  },
};

/** Runs one verb for one request: checks, secret, address, the handler, timeouts. */
export async function handleSession(
  verb: SessionVerb,
  request: Request,
  config: SessionServerConfig,
): Promise<Response> {
  const refusal = checkSessionRequest(request, { appOrigin: config.appOrigin });
  if (refusal) return refusal;
  const read = await readSessionBody(request);
  if ("refusal" in read) return read.refusal;
  if (!config.secret) {
    (config.log ?? ((line: string) => console.warn(line)))(
      "session route: SESSION_FORWARD_SECRET is not set; answering 503",
    );
    return envelopeResponse(503, "session_unavailable", null);
  }
  const clientIp = clientIpFrom(request, {
    environment: config.environment,
    e2eRoutes: config.e2eRoutes,
  });
  if (!clientIp) return envelopeResponse(503, "session_unavailable", null);
  const secret = config.secret;
  const now = config.now ?? (() => Date.now());
  const ctx: Context = {
    config,
    request,
    body: read.body,
    clientIp,
    secret,
    api: (path, options = {}) =>
      callApi({
        apiOrigin: config.apiOrigin,
        path,
        body: options.body,
        bearer: options.bearer,
        cookie: options.cookie,
        clientIp,
        secret,
        now: now(),
        fetch: config.fetch,
        timeoutMs: config.apiTimeoutMs,
      }),
  };
  try {
    return await HANDLERS[verb](ctx);
  } catch (error) {
    if (error instanceof UpstreamTimeout) {
      return envelopeResponse(504, "upstream_timeout", error.requestId);
    }
    return envelopeResponse(502, "dependency_unavailable", null);
  }
}

function methodNotAllowed(): Response {
  return envelopeResponse(405, "method_not_allowed", null, { Allow: "POST" });
}

/** The exports of one app/session/<verb>/route.ts: POST, and 405 for every other method. */
export function sessionRoute(
  verb: SessionVerb,
  config: () => SessionServerConfig = sessionConfigFromEnv,
) {
  const refuse = () => Promise.resolve(methodNotAllowed());
  return {
    POST: (request: Request) => handleSession(verb, request, config()),
    GET: refuse,
    PUT: refuse,
    PATCH: refuse,
    DELETE: refuse,
    OPTIONS: refuse,
  };
}
