/**
 * The browser applications' response headers and CSP (plan section 18, AP-05) - the server
 * half of their security, beside the signer. web and console build the same policy with their
 * own API origin; console has no exception at all, and neither does web in FE-1 (the Embedded
 * Signup exception arrives with its adapter, later).
 */

export type Environment = "development" | "preview" | "production";

export interface CspInput {
  nonce: string;
  apiOrigin: string;
  /** `next dev` needs eval for React's development build; never in a production build. */
  devServer: boolean;
}

export function buildCsp({ nonce, apiOrigin, devServer }: CspInput): string {
  if (!/^[A-Za-z0-9+/=_-]{16,}$/.test(nonce))
    throw new Error("a nonce of at least 16 base64 characters");
  const script = [
    "'self'",
    `'nonce-${nonce}'`,
    "'strict-dynamic'",
    ...(devServer ? ["'unsafe-eval'"] : []),
  ];
  return [
    "default-src 'self'",
    `script-src ${script.join(" ")}`,
    "style-src 'self' 'unsafe-inline'",
    `connect-src 'self' ${apiOrigin}`,
    "img-src 'self' blob: data:",
    "media-src 'self' blob:",
    "font-src 'self'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}

/** Every header but the CSP; Strict-Transport-Security only outside development. */
export function securityHeaders(environment: Environment): Record<string, string> {
  const headers: Record<string, string> = {
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
    "X-Content-Type-Options": "nosniff",
  };
  if (environment !== "development") {
    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains";
  }
  return headers;
}

/** A fresh per-request nonce (128 random bits, base64). */
export function createNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
