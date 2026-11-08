import { NextResponse, type NextRequest } from "next/server";

import { buildCsp, createNonce, securityHeaders } from "@wasla/session/headers";

import { publicEnv } from "./lib/env";

/** Pages reached with a token in the query (reset, invitation, Google's return). */
const TOKEN_PAGES = ["/reset-password", "/invitations/accept", "/auth/google/callback"];

/**
 * Security headers and the per-request CSP nonce - and nothing else (PD-02: never
 * authorization). Next.js reads the nonce from the request's CSP header and puts it on its own
 * scripts; the root layout reads `x-nonce` for any script it renders.
 */
export function proxy(request: NextRequest) {
  const env = publicEnv();
  const nonce = createNonce();
  const csp = buildCsp({
    nonce,
    apiOrigin: env.apiOrigin,
    devServer: process.env.NODE_ENV === "development",
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  for (const [name, value] of Object.entries(securityHeaders(env.environment))) {
    response.headers.set(name, value);
  }
  // Pages that arrive with a token in the URL never pass it on as a referrer (AU-02).
  if (TOKEN_PAGES.includes(request.nextUrl.pathname)) {
    response.headers.set("Referrer-Policy", "no-referrer");
  }
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico|licenses/).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
