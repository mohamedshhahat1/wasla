import { describe, expect, it } from "vitest";

import { buildCsp, createNonce, securityHeaders } from "../src/server/headers";

describe("security headers and CSP (AP-05)", () => {
  const nonce = "AAAAAAAAAAAAAAAAAAAAAA==";

  it("web/console: nonce scripts, API origin only, no frame, no third party", () => {
    const csp = buildCsp({ nonce, apiOrigin: "https://api.iwasla.tech", devServer: false });
    expect(csp).toContain(`script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`);
    expect(csp).toContain("connect-src 'self' https://api.iwasla.tech");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("font-src 'self'");
    expect(csp).not.toContain("unsafe-eval");
    const origins = csp.match(/https?:\/\/[^\s;]+/g) ?? [];
    expect(origins).toEqual(["https://api.iwasla.tech"]);
  });

  it("only next dev gets eval", () => {
    expect(buildCsp({ nonce, apiOrigin: "http://localhost:8000", devServer: true })).toContain(
      "'unsafe-eval'",
    );
  });

  it("refuses a short or odd nonce", () => {
    expect(() => buildCsp({ nonce: "x", apiOrigin: "https://a.b", devServer: false })).toThrow();
    expect(() =>
      buildCsp({ nonce: "'; script-src *", apiOrigin: "https://a.b", devServer: false }),
    ).toThrow();
  });

  it("HSTS only outside development", () => {
    expect(securityHeaders("development")).not.toHaveProperty("Strict-Transport-Security");
    expect(securityHeaders("preview")["Strict-Transport-Security"]).toBe(
      "max-age=63072000; includeSubDomains",
    );
    expect(securityHeaders("production")).toMatchObject({
      "Referrer-Policy": "strict-origin-when-cross-origin",
      "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
      "X-Content-Type-Options": "nosniff",
    });
  });

  it("nonces are fresh and 128-bit", () => {
    const a = createNonce();
    expect(a).toMatch(/^[A-Za-z0-9+/]{22}==$/);
    expect(createNonce()).not.toBe(a);
  });
});
