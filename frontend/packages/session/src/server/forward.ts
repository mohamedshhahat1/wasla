/**
 * The frontend tier's signed forward of the browser's address (FEB-01, ADR-190, SS-03).
 * SERVER-ONLY: imported by FE-2's /session/* route handlers; the lint rule
 * wasla/no-server-only-in-client keeps it out of every client bundle.
 *
 *   canonical = "v1\n" + ts + "\n" + client_ip + "\n" + METHOD + "\n" + path
 *   X-Wasla-Forward-Sig = lowercase hex HMAC-SHA256(SESSION_FORWARD_SECRET, canonical)
 *
 * `path` is the percent-decoded URL path the API receives, without the query string
 * (docs/SECURITY.md, "The browser's address through the frontend tier").
 */
import { createHmac } from "node:crypto";

export const FORWARD_HEADERS = {
  clientIp: "X-Wasla-Client-IP",
  ts: "X-Wasla-Forward-Ts",
  signature: "X-Wasla-Forward-Sig",
} as const;

export interface ForwardInput {
  secret: string;
  /** The browser's address as the tier received it (on Cloudflare, CF-Connecting-IP). */
  clientIp: string;
  method: string;
  /** The percent-decoded path the API will see; use `forwardPath()` for a URL. */
  path: string;
  /** The signing time (milliseconds since the epoch, or a Date); injected, never read here. */
  now: number | Date;
}

export type ForwardHeaders = Record<(typeof FORWARD_HEADERS)[keyof typeof FORWARD_HEADERS], string>;

/** The decoded path of a URL, without its query, as the API (Starlette) will see it. */
export function forwardPath(url: string | URL): string {
  const parsed = typeof url === "string" ? new URL(url, "http://forward.invalid") : url;
  return decodeURIComponent(parsed.pathname);
}

export function canonicalString(
  ts: string,
  clientIp: string,
  method: string,
  path: string,
): string {
  return `v1\n${ts}\n${clientIp}\n${method}\n${path}`;
}

export function signForward(input: ForwardInput): ForwardHeaders {
  if (!input.secret) throw new Error("the forward secret is not set");
  if (!input.clientIp || /\s/.test(input.clientIp)) throw new Error("not a client address");
  if (!input.path.startsWith("/") || input.path.includes("?")) {
    throw new Error("the signed path is a decoded path without a query");
  }
  const method = input.method.toLocaleUpperCase("en");
  const milliseconds = input.now instanceof Date ? input.now.getTime() : input.now;
  const ts = String(Math.floor(milliseconds / 1000));
  const signature = createHmac("sha256", input.secret)
    .update(canonicalString(ts, input.clientIp, method, input.path), "utf8")
    .digest("hex");
  return {
    [FORWARD_HEADERS.clientIp]: input.clientIp,
    [FORWARD_HEADERS.ts]: ts,
    [FORWARD_HEADERS.signature]: signature,
  };
}
