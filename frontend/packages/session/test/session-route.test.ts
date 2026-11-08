/* eslint-disable @typescript-eslint/require-await -- test doubles implement async interfaces without awaiting */
import { createHmac } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  callApi,
  checkSessionRequest,
  clearBindingCookieHeaders,
  clearRefreshCookieHeader,
  clientIpFrom,
  E2E_CLIENT_IP_HEADER,
  MAX_SESSION_BODY_BYTES,
  passThroughError,
  presentedBinding,
  readCookie,
  readSessionBody,
  refreshCookieHeader,
  refreshCookieName,
  relayBindingCookie,
  UpstreamTimeout,
} from "../src/server";
import { canonicalString } from "../src/server/forward";

const APP = "https://app.iwasla.tech";

function request(init: {
  method?: string;
  origin?: string | null;
  site?: string | null;
  session?: string | null;
  type?: string | null;
  body?: string;
  headers?: Record<string, string>;
}): Request {
  const headers = new Headers(init.headers ?? {});
  if (init.origin !== null) headers.set("Origin", init.origin ?? APP);
  if (init.site !== null) headers.set("Sec-Fetch-Site", init.site ?? "same-origin");
  if (init.session !== null) headers.set("X-Wasla-Session", init.session ?? "1");
  if (init.type !== null) headers.set("Content-Type", init.type ?? "application/json");
  const method = init.method ?? "POST";
  return new Request(`${APP}/session/login`, {
    method,
    headers,
    ...(method === "GET" || method === "HEAD" ? {} : { body: init.body ?? "{}" }),
  });
}

async function codeOf(response: Response | null): Promise<string | null> {
  if (!response) return null;
  const body = (await response.json()) as { error: { code: string } };
  return body.error.code;
}

describe("checkSessionRequest (SR-02)", () => {
  const check = (r: Request) => checkSessionRequest(r, { appOrigin: APP });

  it("accepts a same-origin POST with the header and JSON", () => {
    expect(check(request({}))).toBeNull();
  });

  it("accepts it without Sec-Fetch-Site (an older engine): the Origin check decides", () => {
    expect(check(request({ site: null }))).toBeNull();
  });

  it.each(["GET", "PUT", "PATCH", "DELETE", "OPTIONS"])(
    "refuses %s with 405 and Allow: POST",
    async (method) => {
      const refusal = check(request({ method }));
      expect(refusal?.status).toBe(405);
      expect(refusal?.headers.get("Allow")).toBe("POST");
      expect(await codeOf(refusal)).toBe("method_not_allowed");
    },
  );

  const refusals: Array<[string, Parameters<typeof request>[0]]> = [
    ["no Origin", { origin: null }],
    ["another origin", { origin: "https://evil.example" }],
    ["a prefix of the origin", { origin: "https://app.iwasla.tech.evil.example" }],
    ["the origin over http", { origin: "http://app.iwasla.tech" }],
    ["another port", { origin: "https://app.iwasla.tech:8443" }],
    ["the null origin", { origin: "null" }],
    ["Sec-Fetch-Site: cross-site", { site: "cross-site" }],
    ["Sec-Fetch-Site: same-site", { site: "same-site" }],
    ["Sec-Fetch-Site: none", { site: "none" }],
    ["no X-Wasla-Session", { session: null }],
    ["X-Wasla-Session: 0", { session: "0" }],
    ["no Content-Type", { type: null }],
    ["a form post", { type: "application/x-www-form-urlencoded" }],
    ["text/plain (a simple request)", { type: "text/plain" }],
    ["cross-site even with the header", { origin: "https://evil.example", site: "cross-site" }],
  ];
  it.each(refusals)("refuses %s with 403 session_request_refused", async (_name, init) => {
    const refusal = check(request(init));
    expect(refusal?.status).toBe(403);
    expect(await codeOf(refusal)).toBe("session_request_refused");
    expect(refusal?.headers.get("Cache-Control")).toBe("no-store");
    expect(refusal?.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("accepts application/json with a charset", () => {
    expect(check(request({ type: "application/json; charset=utf-8" }))).toBeNull();
  });
});

describe("readSessionBody", () => {
  it("reads a JSON object; an empty body is {}", async () => {
    expect(await readSessionBody(request({ body: '{"a":1}' }))).toEqual({ body: { a: 1 } });
    expect(await readSessionBody(request({ body: "" }))).toEqual({ body: {} });
  });

  it("refuses more than 16 KiB with 413, and not-an-object with 400", async () => {
    const big = await readSessionBody(
      request({ body: JSON.stringify({ x: "y".repeat(MAX_SESSION_BODY_BYTES) }) }),
    );
    expect("refusal" in big && big.refusal.status).toBe(413);
    for (const body of ["[1]", "not json", "1"]) {
      const read = await readSessionBody(request({ body }));
      expect("refusal" in read && read.refusal.status).toBe(400);
    }
  });
});

describe("the refresh cookie (SR-03)", () => {
  it("is __Host- over HTTPS and plain over http development", () => {
    expect(refreshCookieName("production")).toBe("__Host-wasla_rt");
    expect(refreshCookieName("preview")).toBe("__Host-wasla_rt");
    expect(refreshCookieName("development")).toBe("wasla_rt");
  });

  it("is HttpOnly, SameSite=Strict, Path=/, host-only, Secure outside development", () => {
    const header = refreshCookieHeader("production", "rt.abc", 1_209_600);
    expect(header).toBe(
      "__Host-wasla_rt=rt.abc; Path=/; Max-Age=1209600; HttpOnly; SameSite=Strict; Secure",
    );
    expect(header).not.toMatch(/Domain/i);
    const dev = refreshCookieHeader("development", "rt.abc", 60);
    expect(dev).toBe("wasla_rt=rt.abc; Path=/; Max-Age=60; HttpOnly; SameSite=Strict");
  });

  it("is cleared with the same name and attributes", () => {
    expect(clearRefreshCookieHeader("production")).toBe(
      "__Host-wasla_rt=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict; Secure",
    );
  });

  it("refuses a value that would break the header", () => {
    expect(() => refreshCookieHeader("production", "a;b", 1)).toThrow();
    expect(() => refreshCookieHeader("production", "", 1)).toThrow();
  });

  it("reads one cookie from the Cookie header", () => {
    const r = new Request(APP, { headers: { Cookie: "a=1; wasla_rt=tok.en; b=2" } });
    expect(readCookie(r, "wasla_rt")).toBe("tok.en");
    expect(readCookie(r, "missing")).toBeNull();
  });
});

describe("clientIpFrom (SR-04)", () => {
  const ip = (
    headers: Record<string, string>,
    environment: "development" | "production",
    e2eRoutes = false,
  ) => clientIpFrom(new Request(APP, { headers }), { environment, e2eRoutes });

  it("on Cloudflare: CF-Connecting-IP only, never X-Forwarded-For or the test header", () => {
    expect(ip({ "CF-Connecting-IP": "203.0.113.7" }, "production")).toBe("203.0.113.7");
    expect(ip({ "X-Forwarded-For": "198.51.100.1" }, "production")).toBeNull();
    expect(ip({ [E2E_CLIENT_IP_HEADER]: "198.51.100.9" }, "production", true)).toBeNull();
    expect(
      ip(
        { "CF-Connecting-IP": "203.0.113.7", [E2E_CLIENT_IP_HEADER]: "198.51.100.9" },
        "production",
        true,
      ),
    ).toBe("203.0.113.7");
  });

  it("in development: the e2e header only with the flag, else the forwarded address, else 127.0.0.1", () => {
    expect(ip({ [E2E_CLIENT_IP_HEADER]: "198.51.100.9" }, "development", true)).toBe(
      "198.51.100.9",
    );
    expect(ip({ [E2E_CLIENT_IP_HEADER]: "198.51.100.9" }, "development", false)).toBe("127.0.0.1");
    expect(ip({ "X-Forwarded-For": "::1, 10.0.0.1" }, "development")).toBe("::1");
    expect(ip({}, "development")).toBe("127.0.0.1");
    expect(ip({ [E2E_CLIENT_IP_HEADER]: "not an address!" }, "development", true)).toBe(
      "127.0.0.1",
    );
  });
});

describe("callApi (SR-04, SR-09)", () => {
  it("signs with FE-0's algorithm, sends a request id, and only calls the API origin", async () => {
    const seen: Request[] = [];
    const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      seen.push(new Request(input, init));
      return new Response('{"ok":true}', { status: 200, headers: { "X-Request-ID": "rid-1" } });
    };
    const answer = await callApi({
      apiOrigin: "https://api.iwasla.tech",
      path: "/api/v1/auth/login",
      body: { email: "a@b.c" },
      clientIp: "203.0.113.7",
      secret: "s3cret",
      now: 1_700_000_000_000,
      fetch,
    });
    expect(answer).toMatchObject({ status: 200, json: { ok: true }, requestId: "rid-1" });
    const sent = seen[0]!;
    expect(new URL(sent.url).origin).toBe("https://api.iwasla.tech");
    expect(sent.headers.get("X-Wasla-Client-IP")).toBe("203.0.113.7");
    const ts = sent.headers.get("X-Wasla-Forward-Ts")!;
    const expected = createHmac("sha256", "s3cret")
      .update(canonicalString(ts, "203.0.113.7", "POST", "/api/v1/auth/login"))
      .digest("hex");
    expect(sent.headers.get("X-Wasla-Forward-Sig")).toBe(expected);
    expect(sent.headers.get("X-Request-ID")).toMatch(/^[0-9a-f-]{36}$/);
    expect(sent.headers.get("Cookie")).toBeNull();
  });

  it("refuses a path that would leave the API origin", async () => {
    await expect(
      callApi({
        apiOrigin: "https://api.iwasla.tech",
        path: "https://evil.example/x",
        clientIp: "203.0.113.7",
        secret: "s",
        now: 0,
        fetch: async () => new Response(null),
      }),
    ).rejects.toThrow(/left the API origin/);
  });

  it("times out after the given time as UpstreamTimeout", async () => {
    const hang = (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    await expect(
      callApi({
        apiOrigin: "https://api.iwasla.tech",
        path: "/x",
        clientIp: "1.2.3.4",
        secret: "s",
        now: 0,
        fetch: hang,
        timeoutMs: 5,
      }),
    ).rejects.toBeInstanceOf(UpstreamTimeout);
  });

  it("passes an error through by code, request id and Retry-After - never the sentence", async () => {
    const response = passThroughError({
      status: 429,
      json: { error: { code: "rate_limited", message: "Try again in 50 seconds." } },
      headers: new Headers({ "Retry-After": "50" }),
      requestId: "rid-9",
    });
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("50");
    expect(response.headers.get("X-Request-ID")).toBe("rid-9");
    const text = await response.text();
    expect(text).not.toContain("Try again");
    expect(JSON.parse(text)).toEqual({
      error: { code: "rate_limited", message: "rate_limited", request_id: "rid-9" },
    });
  });

  it("an answer that is not the envelope is unexpected_response", async () => {
    const response = passThroughError({
      status: 502,
      json: null,
      headers: new Headers(),
      requestId: "r",
    });
    expect(await codeOf(response)).toBe("unexpected_response");
  });
});

describe("the Google binding cookie relay (SR-07, FEB-21)", () => {
  it("relays __Host-wasla_oauth under its name, Path=/ (the prefix requires it), host-only", () => {
    const relayed = relayBindingCookie([
      "__Host-wasla_oauth=bind123; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure",
    ]);
    expect(relayed).toEqual({
      name: "__Host-wasla_oauth",
      setCookie: "__Host-wasla_oauth=bind123; Path=/; Max-Age=600; HttpOnly; SameSite=lax; Secure",
    });
  });

  it("relays the plain-HTTP wasla_oauth scoped to /session/google/", () => {
    const relayed = relayBindingCookie([
      "wasla_oauth=b; HttpOnly; Max-Age=600; Path=/; SameSite=lax",
    ]);
    expect(relayed?.setCookie).toBe(
      "wasla_oauth=b; Path=/session/google/; Max-Age=600; HttpOnly; SameSite=lax",
    );
    expect(relayed?.setCookie).not.toMatch(/Domain/i);
  });

  it("refuses any other cookie name", () => {
    expect(relayBindingCookie(["session=x; Path=/", "wasla_oauth2=y"])).toBeNull();
  });

  it("finds the binding the browser sends back under either name, and clears both", () => {
    const r = new Request(APP, { headers: { Cookie: "x=1; wasla_oauth=b" } });
    expect(presentedBinding(r)).toEqual({ name: "wasla_oauth", value: "b" });
    expect(presentedBinding(new Request(APP, { headers: { Cookie: "other_oauth=b" } }))).toBeNull();
    expect(clearBindingCookieHeaders()).toHaveLength(2);
  });
});
