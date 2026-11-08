/**
 * The framework-free half of the S-2 route handlers (FE-2, SR-01..SR-09, ADR-193). SERVER-ONLY.
 * apps/web's /session/* handlers are thin compositions of these: the request checks first
 * (CSRF, plan section 22), then one signed call to the API, then the refresh cookie.
 *
 * Every refusal is the API's own envelope shape with a frontend code, so the page reads it with
 * the same normaliser (AC-03) and shows a translated sentence by code.
 */
import { randomUUID } from "node:crypto";

import { forwardPath, signForward } from "./forward";

export type WaslaEnvironment = "development" | "preview" | "production";

/** Frontend error codes the session routes answer with (catalogued in errors.json). */
export const SESSION_ROUTE_CODES = [
  "session_request_refused",
  "session_unavailable",
  "upstream_timeout",
] as const;

/** The refresh cookie: `__Host-` wherever it can be (HTTPS), the plain name over plain-HTTP development. */
export const REFRESH_COOKIE_SECURE = "__Host-wasla_rt";
export const REFRESH_COOKIE_PLAIN = "wasla_rt";

/** The API's Google binding cookie, under the two names it uses (FEB-21) - and no other. */
export const BINDING_COOKIE_NAMES = ["__Host-wasla_oauth", "wasla_oauth"] as const;
export type BindingCookieName = (typeof BINDING_COOKIE_NAMES)[number];

/** The largest body a session route reads (SR-02). */
export const MAX_SESSION_BODY_BYTES = 16 * 1024;

/** How long a session route waits for the API (SR-04). */
export const API_TIMEOUT_MS = 10_000;

/** The refresh token's lifetime when the API does not state it: the API's default (FEB-30). */
export const DEFAULT_REFRESH_LIFETIME_SECONDS = 1_209_600;

/** Headers every /session/* answer carries (SR-08). */
export function sessionResponseHeaders(extra: HeadersInit = {}): Headers {
  const headers = new Headers(extra);
  headers.set("Cache-Control", "no-store");
  headers.set("Content-Type", "application/json");
  headers.append("Vary", "Origin");
  return headers;
}

/** The API's error envelope with a frontend code. */
export function envelopeResponse(
  status: number,
  code: string,
  requestId: string | null,
  extraHeaders: HeadersInit = {},
): Response {
  const headers = sessionResponseHeaders(extraHeaders);
  if (requestId) headers.set("X-Request-ID", requestId);
  return new Response(JSON.stringify({ error: { code, message: code, request_id: requestId } }), {
    status,
    headers,
  });
}

export interface SessionRequestCheck {
  /** The app's own origin exactly (scheme, host, port). */
  appOrigin: string;
}

/**
 * SR-02, in order: POST (405), Origin equal to the app origin (403), Sec-Fetch-Site same-origin
 * when the browser sends it (403; absent → the Origin check decides), X-Wasla-Session: 1 (403),
 * Content-Type application/json (403). Returns the refusal, or null when the request may go on.
 * Nothing here reads the body or the cookie, and nothing calls the API.
 */
export function checkSessionRequest(request: Request, check: SessionRequestCheck): Response | null {
  if (request.method !== "POST") {
    return envelopeResponse(405, "method_not_allowed", null, { Allow: "POST" });
  }
  const refuse = () => envelopeResponse(403, "session_request_refused", null);
  const origin = request.headers.get("Origin");
  if (origin === null || origin !== check.appOrigin) return refuse();
  const site = request.headers.get("Sec-Fetch-Site");
  if (site !== null && site !== "same-origin") return refuse();
  if (request.headers.get("X-Wasla-Session") !== "1") return refuse();
  const type = request.headers.get("Content-Type") ?? "";
  if (type.split(";")[0]?.trim().toLowerCase() !== "application/json") return refuse();
  return null;
}

/**
 * The JSON object body of a checked request, at most 16 KiB (413 above, 400 if it is not a JSON
 * object). An empty body reads as {}.
 */
export async function readSessionBody(
  request: Request,
): Promise<{ body: Record<string, unknown> } | { refusal: Response }> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (declared > MAX_SESSION_BODY_BYTES) {
    return { refusal: envelopeResponse(413, "payload_too_large", null) };
  }
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.byteLength > MAX_SESSION_BODY_BYTES) {
    return { refusal: envelopeResponse(413, "payload_too_large", null) };
  }
  if (bytes.byteLength === 0) return { body: {} };
  try {
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
    return { body: parsed as Record<string, unknown> };
  } catch {
    return { refusal: envelopeResponse(400, "bad_request", null) };
  }
}

/** SR-03: the refresh cookie's name. Outside development it must carry the `__Host-` prefix. */
export function refreshCookieName(environment: WaslaEnvironment): string {
  const name = environment === "development" ? REFRESH_COOKIE_PLAIN : REFRESH_COOKIE_SECURE;
  if (environment !== "development" && !name.startsWith("__Host-")) {
    throw new Error("outside development the refresh cookie must be a __Host- cookie");
  }
  return name;
}

/** `Set-Cookie` for the refresh token: HttpOnly, SameSite=Strict, Path=/, host-only, Secure over HTTPS. */
export function refreshCookieHeader(
  environment: WaslaEnvironment,
  value: string,
  maxAgeSeconds: number,
): string {
  if (!value || /[;\s,]/.test(value)) throw new Error("not a cookie-safe refresh token");
  const secure = environment === "development" ? "" : "; Secure";
  return `${refreshCookieName(environment)}=${value}; Path=/; Max-Age=${Math.max(0, Math.floor(maxAgeSeconds))}; HttpOnly; SameSite=Strict${secure}`;
}

/** The same cookie, expired. */
export function clearRefreshCookieHeader(environment: WaslaEnvironment): string {
  const secure = environment === "development" ? "" : "; Secure";
  return `${refreshCookieName(environment)}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict${secure}`;
}

/** One cookie's value from a request's Cookie header. */
export function readCookie(request: Request, name: string): string | null {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    if (part.slice(0, index).trim() === name) return part.slice(index + 1).trim() || null;
  }
  return null;
}

export interface ClientIpOptions {
  environment: WaslaEnvironment;
  /** WASLA_E2E_ROUTES=1 at start - honoured only by a development build. */
  e2eRoutes: boolean;
}

export const E2E_CLIENT_IP_HEADER = "X-Wasla-E2E-Client-IP";

const ADDRESS = /^[0-9a-fA-F:.]{2,45}$/;

/**
 * SR-04: the browser's address as the tier saw it. On Cloudflare (preview, production) only
 * CF-Connecting-IP, which Cloudflare's edge sets and a browser cannot keep; null without it (the
 * route then refuses). In development: the e2e test header (only with WASLA_E2E_ROUTES=1), else
 * the address `next start` forwards (X-Forwarded-For), else 127.0.0.1.
 */
export function clientIpFrom(request: Request, options: ClientIpOptions): string | null {
  const valid = (value: string | null | undefined) =>
    value && ADDRESS.test(value.trim()) ? value.trim() : null;
  if (options.environment !== "development") {
    return valid(request.headers.get("CF-Connecting-IP"));
  }
  if (options.e2eRoutes) {
    const forced = valid(request.headers.get(E2E_CLIENT_IP_HEADER));
    if (forced) return forced;
  }
  const forwarded = valid(request.headers.get("X-Forwarded-For")?.split(",")[0]);
  return forwarded ?? "127.0.0.1";
}

export interface ApiCall {
  apiOrigin: string;
  /** The API path, e.g. /api/v1/auth/login. */
  path: string;
  body?: Record<string, unknown> | undefined;
  bearer?: string | null | undefined;
  /** A Cookie header for the API (only the Google binding cookie, SR-07). */
  cookie?: string | null | undefined;
  clientIp: string;
  secret: string;
  now: number;
  fetch?: typeof globalThis.fetch | undefined;
  timeoutMs?: number | undefined;
  requestId?: string | undefined;
}

export interface ApiAnswer {
  status: number;
  /** The parsed JSON body, or null. */
  json: unknown;
  headers: Headers;
  requestId: string;
}

export class UpstreamTimeout extends Error {
  readonly requestId: string;
  constructor(requestId: string) {
    super("upstream_timeout");
    this.requestId = requestId;
  }
}

/**
 * One POST to the API, server to server, signed with the forward headers (FEB-01) so the API
 * counts the browser's address, with a new X-Request-ID and a 10 s timeout. Only `apiOrigin`
 * is ever called (SR-09).
 */
export async function callApi(call: ApiCall): Promise<ApiAnswer> {
  const url = new URL(call.path, call.apiOrigin);
  if (url.origin !== new URL(call.apiOrigin).origin)
    throw new Error("the API call left the API origin");
  const requestId = call.requestId ?? randomUUID();
  const headers = new Headers({
    Accept: "application/json",
    "X-Request-ID": requestId,
    ...signForward({
      secret: call.secret,
      clientIp: call.clientIp,
      method: "POST",
      path: forwardPath(url),
      now: call.now,
    }),
  });
  if (call.body !== undefined) headers.set("Content-Type", "application/json");
  if (call.bearer) headers.set("Authorization", `Bearer ${call.bearer}`);
  if (call.cookie) headers.set("Cookie", call.cookie);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), call.timeoutMs ?? API_TIMEOUT_MS);
  const doFetch = call.fetch ?? globalThis.fetch;
  try {
    const init: RequestInit = {
      method: "POST",
      headers,
      signal: controller.signal,
      redirect: "manual",
    };
    if (call.body !== undefined) init.body = JSON.stringify(call.body);
    const response = await doFetch(url, init);
    const text = await response.text();
    let json: unknown = null;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        json = null;
      }
    }
    return {
      status: response.status,
      json,
      headers: response.headers,
      requestId: response.headers.get("X-Request-ID") ?? requestId,
    };
  } catch (error) {
    if (controller.signal.aborted) throw new UpstreamTimeout(requestId);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * An API error answer passed through to the page: its status, its code, its request id and its
 * Retry-After - never its English sentence (AU-04). A body that is not the envelope becomes
 * `unexpected_response`.
 */
export function passThroughError(answer: ApiAnswer, extraHeaders: HeadersInit = {}): Response {
  const envelope =
    typeof answer.json === "object" && answer.json !== null
      ? (answer.json as { error?: { code?: unknown; details?: unknown } }).error
      : undefined;
  const code =
    typeof envelope?.code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(envelope.code)
      ? envelope.code
      : "unexpected_response";
  const headers = sessionResponseHeaders(extraHeaders);
  headers.set("X-Request-ID", answer.requestId);
  const retryAfter = answer.headers.get("Retry-After");
  if (retryAfter) headers.set("Retry-After", retryAfter);
  const error: Record<string, unknown> = { code, message: code, request_id: answer.requestId };
  // 422 details carry field locations and error types (no sentence the UI shows).
  if (answer.status === 422 && typeof envelope?.details === "object")
    error["details"] = envelope.details;
  return new Response(JSON.stringify({ error }), { status: answer.status, headers });
}

interface ParsedSetCookie {
  name: string;
  value: string;
  attributes: Map<string, string | true>;
}

function parseSetCookie(header: string): ParsedSetCookie | null {
  const [pair, ...rest] = header.split(";");
  const index = pair?.indexOf("=") ?? -1;
  if (!pair || index <= 0) return null;
  const attributes = new Map<string, string | true>();
  for (const raw of rest) {
    const part = raw.trim();
    if (!part) continue;
    const eq = part.indexOf("=");
    if (eq < 0) attributes.set(part.toLowerCase(), true);
    else attributes.set(part.slice(0, eq).trim().toLowerCase(), part.slice(eq + 1).trim());
  }
  return { name: pair.slice(0, index).trim(), value: pair.slice(index + 1).trim(), attributes };
}

export interface RelayedBinding {
  name: BindingCookieName;
  /** The Set-Cookie for the app's own origin. */
  setCookie: string;
}

/**
 * SR-07: the API's binding cookie, copied onto the app origin under the SAME name, host-only,
 * HttpOnly, the API's SameSite and Max-Age. `__Host-wasla_oauth` must keep Path=/ (the prefix
 * requires it); the plain-HTTP `wasla_oauth` is scoped to /session/google/. Any other cookie name
 * is refused (null).
 */
export function relayBindingCookie(setCookieHeaders: string[]): RelayedBinding | null {
  for (const header of setCookieHeaders) {
    const parsed = parseSetCookie(header);
    if (!parsed) continue;
    if (!(BINDING_COOKIE_NAMES as readonly string[]).includes(parsed.name)) continue;
    if (!parsed.value || /[;\s,]/.test(parsed.value)) return null;
    const name = parsed.name as BindingCookieName;
    const secure = name.startsWith("__Host-");
    const path = secure ? "/" : "/session/google/";
    const sameSite = parsed.attributes.get("samesite");
    const maxAge = parsed.attributes.get("max-age");
    const parts = [`${name}=${parsed.value}`, `Path=${path}`];
    if (typeof maxAge === "string" && /^\d+$/.test(maxAge)) parts.push(`Max-Age=${maxAge}`);
    parts.push("HttpOnly");
    parts.push(`SameSite=${typeof sameSite === "string" ? sameSite : "Lax"}`);
    if (secure) parts.push("Secure");
    return { name, setCookie: parts.join("; ") };
  }
  return null;
}

/** The binding cookie the browser sent back to the callback route, under either name. */
export function presentedBinding(
  request: Request,
): { name: BindingCookieName; value: string } | null {
  for (const name of BINDING_COOKIE_NAMES) {
    const value = readCookie(request, name);
    if (value && !/[;\s,]/.test(value)) return { name, value };
  }
  return null;
}

/** Expires both binding-cookie names on the app origin, whatever the result (SR-07). */
export function clearBindingCookieHeaders(): string[] {
  return [
    "__Host-wasla_oauth=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure",
    "wasla_oauth=; Path=/session/google/; Max-Age=0; HttpOnly; SameSite=Lax",
  ];
}
