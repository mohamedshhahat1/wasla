/**
 * The eight /session/* handlers against a fake API (FE-2 section 13.3): per route the API path
 * called, the forward signature, the cookie set or cleared, a body that never carries the
 * refresh token, errors passed through by code, the timeout and the missing secret.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { signForward } from "@wasla/session/server";

import {
  handleSession,
  SESSION_VERBS,
  sessionRoute,
  type SessionServerConfig,
  type SessionVerb,
} from "../lib/server/session-handlers";
import { config as proxyConfig } from "../proxy";

const APP = "http://localhost:3000";
const API = "http://localhost:8000";
const SECRET = "test-forward-secret";
const NOW = Date.UTC(2026, 10, 8, 10, 0, 0);
const REFRESH = "rt-secret-value-never-in-a-body";

interface Seen {
  url: string;
  method: string;
  headers: Headers;
  body: Record<string, unknown> | null;
}

type Reply = { status: number; json?: unknown; setCookie?: string[]; delayForever?: boolean };

function fakeApi(replies: Record<string, Reply>) {
  const seen: Seen[] = [];
  const fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = input instanceof Request ? input.url : input.toString();
    const headers = new Headers(init?.headers);
    const raw = typeof init?.body === "string" ? init.body : null;
    seen.push({
      url,
      method: init?.method ?? "GET",
      headers,
      body: raw ? (JSON.parse(raw) as Record<string, unknown>) : null,
    });
    const path = new URL(url).pathname;
    const reply = replies[path];
    if (!reply) return Promise.resolve(new Response("not faked", { status: 599 }));
    if (reply.delayForever) {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    const responseHeaders = new Headers({
      "Content-Type": "application/json",
      "X-Request-ID": "req-from-api",
    });
    for (const cookie of reply.setCookie ?? []) responseHeaders.append("Set-Cookie", cookie);
    return Promise.resolve(
      new Response(reply.json === undefined ? null : JSON.stringify(reply.json), {
        status: reply.status,
        headers: responseHeaders,
      }),
    );
  };
  return { seen, fetch };
}

function config(fetch: typeof globalThis.fetch, extra: Partial<SessionServerConfig> = {}) {
  const lines: string[] = [];
  const value: SessionServerConfig = {
    environment: "development",
    appOrigin: APP,
    apiOrigin: API,
    secret: SECRET,
    e2eRoutes: false,
    fetch,
    now: () => NOW,
    log: (line) => lines.push(line),
    ...extra,
  };
  return { value, lines };
}

function post(verb: SessionVerb, body: unknown = {}, headers: Record<string, string> = {}) {
  return new Request(`${APP}/session/${verb}`, {
    method: "POST",
    headers: {
      Origin: APP,
      "Sec-Fetch-Site": "same-origin",
      "X-Wasla-Session": "1",
      "Content-Type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

const session = {
  access_token: "access-1",
  refresh_token: REFRESH,
  expires_in: 900,
  token_type: "bearer",
  active_workspace: { id: "w1", slug: "acme", name: "Acme", role: "owner" },
};

const bodyText = async (response: Response) => response.clone().text();

describe("every route refuses before any API call (SR-02)", () => {
  const refusals: Array<[string, RequestInit & { headers: Record<string, string> }, number]> = [
    ["another origin", { headers: { Origin: "http://127.0.0.1:3000" } }, 403],
    ["a suffix of the origin", { headers: { Origin: `${APP}.evil.example` } }, 403],
    ["no Origin", { headers: { Origin: "" } }, 403],
    ["Sec-Fetch-Site cross-site", { headers: { "Sec-Fetch-Site": "cross-site" } }, 403],
    ["no session header", { headers: { "X-Wasla-Session": "" } }, 403],
    ["a form body", { headers: { "Content-Type": "application/x-www-form-urlencoded" } }, 403],
  ];
  for (const verb of SESSION_VERBS) {
    for (const [name, init, status] of refusals) {
      it(`${verb}: ${name} → ${status}, no API call`, async () => {
        const api = fakeApi({});
        const headers = new Headers({
          Origin: APP,
          "Sec-Fetch-Site": "same-origin",
          "X-Wasla-Session": "1",
          "Content-Type": "application/json",
        });
        for (const [key, value] of Object.entries(init.headers)) {
          if (value === "") headers.delete(key);
          else headers.set(key, value);
        }
        const request = new Request(`${APP}/session/${verb}`, {
          method: "POST",
          headers,
          body: "{}",
        });
        const response = await handleSession(verb, request, config(api.fetch).value);
        expect(response.status).toBe(status);
        expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
          "session_request_refused",
        );
        expect(api.seen).toEqual([]);
      });
    }

    it(`${verb}: GET → 405 with Allow: POST; a body over 16 KiB → 413`, async () => {
      const api = fakeApi({});
      const route = sessionRoute(verb, () => config(api.fetch).value);
      const get = await route.GET();
      expect(get.status).toBe(405);
      expect(get.headers.get("Allow")).toBe("POST");
      const big = await route.POST(post(verb, { email: "x".repeat(17 * 1024) }));
      expect(big.status).toBe(413);
      expect(api.seen).toEqual([]);
    });

    it(`${verb}: no SESSION_FORWARD_SECRET → 503 session_unavailable, one line without a value`, async () => {
      const api = fakeApi({});
      const { value, lines } = config(api.fetch, { secret: null });
      const response = await handleSession(verb, post(verb, {}), value);
      expect(response.status).toBe(503);
      expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
        "session_unavailable",
      );
      expect(api.seen).toEqual([]);
      expect(lines).toHaveLength(1);
      expect(lines[0]).not.toContain(SECRET);
    });
  }

  it("every route module is the shared handler (the checks run first in each)", () => {
    const root = join(__dirname, "..", "app", "session");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (entry.name === "route.ts") files.push(path);
      }
    };
    walk(root);
    expect(files).toHaveLength(SESSION_VERBS.length);
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      expect(source, file).toMatch(/sessionRoute\("[a-z/-]+"\)/);
      expect(source, file).toContain('export const runtime = "nodejs"');
    }
  });
});

describe("the responses (SR-08)", () => {
  it("no-store, JSON, Vary: Origin, and no CORS header", async () => {
    const api = fakeApi({ "/api/v1/auth/login": { status: 200, json: session } });
    const response = await handleSession("login", post("login", {}), config(api.fetch).value);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(response.headers.get("Content-Type")).toContain("application/json");
    expect(response.headers.get("Vary")).toContain("Origin");
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("the security headers come from proxy.ts, whose matcher covers every /session/* path", () => {
    const source = proxyConfig.matcher[0]!.source;
    const pattern = new RegExp(`^${source}$`);
    for (const verb of SESSION_VERBS) expect(pattern.test(`/session/${verb}`), verb).toBe(true);
  });
});

describe("login (SR-05)", () => {
  it("calls POST /auth/login signed, sets the cookie, and never returns the refresh token", async () => {
    const api = fakeApi({ "/api/v1/auth/login": { status: 200, json: session } });
    const response = await handleSession(
      "login",
      post("login", { email: "a@example.com", password: "pw", workspace_slug: "acme", extra: "x" }),
      config(api.fetch).value,
    );
    expect(response.status).toBe(200);
    expect(api.seen).toHaveLength(1);
    const call = api.seen[0]!;
    expect(call.url).toBe(`${API}/api/v1/auth/login`);
    expect(call.method).toBe("POST");
    expect(call.body).toEqual({ email: "a@example.com", password: "pw", workspace_slug: "acme" });
    const expected = signForward({
      secret: SECRET,
      clientIp: "127.0.0.1",
      method: "POST",
      path: "/api/v1/auth/login",
      now: NOW,
    });
    for (const [name, value] of Object.entries(expected))
      expect(call.headers.get(name)).toBe(value);
    expect(call.headers.get("X-Request-ID")).toMatch(/^[0-9a-f-]{36}$/);
    expect(call.headers.get("Cookie")).toBeNull();

    const cookie = response.headers.getSetCookie();
    expect(cookie).toHaveLength(1);
    expect(cookie[0]).toMatch(/^wasla_rt=rt-secret-value-never-in-a-body; /);
    expect(cookie[0]).toContain("HttpOnly");
    expect(cookie[0]).toContain("SameSite=Strict");
    expect(cookie[0]).toContain("Path=/");
    expect(cookie[0]).not.toContain("Domain");
    const text = await bodyText(response);
    expect(text).not.toContain(REFRESH);
    expect(JSON.parse(text)).toEqual({
      access_token: "access-1",
      expires_in: 900,
      active_workspace: session.active_workspace,
    });
  });

  it("passes a 401 through by code and request id, with no cookie and no sentence", async () => {
    const api = fakeApi({
      "/api/v1/auth/login": {
        status: 401,
        json: { error: { code: "invalid_credentials", message: "Invalid email or password." } },
      },
    });
    const response = await handleSession("login", post("login", {}), config(api.fetch).value);
    expect(response.status).toBe(401);
    const text = await bodyText(response);
    expect(text).toContain("invalid_credentials");
    expect(text).not.toContain("Invalid email or password");
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("passes a 429 through with Retry-After", async () => {
    const replies: Record<string, Reply> = {
      "/api/v1/auth/login": {
        status: 429,
        json: { error: { code: "rate_limited", message: "Too many requests." } },
      },
    };
    const api = fakeApi(replies);
    const wrapped: typeof fetch = async (input, init) => {
      const response = await api.fetch(input, init);
      const headers = new Headers(response.headers);
      headers.set("Retry-After", "42");
      return new Response(await response.text(), { status: response.status, headers });
    };
    const response = await handleSession("login", post("login", {}), config(wrapped).value);
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("42");
  });

  it("an API that does not answer in time → 504 upstream_timeout", async () => {
    const api = fakeApi({ "/api/v1/auth/login": { status: 200, delayForever: true } });
    const started = Date.now();
    const response = await handleSession("login", post("login", {}), {
      ...config(api.fetch).value,
      apiTimeoutMs: 50,
    });
    // The production timeout is 10 s (API_TIMEOUT_MS); a test-sized one is injected.
    expect(response.status).toBe(504);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "upstream_timeout",
    );
    expect(Date.now() - started).toBeLessThan(5000);
  });

  it("signs the e2e test address only when the e2e routes are on in development", async () => {
    const api = fakeApi({ "/api/v1/auth/login": { status: 200, json: session } });
    const header = { "X-Wasla-E2E-Client-IP": "203.0.113.7" };
    await handleSession(
      "login",
      post("login", {}, header),
      config(api.fetch, { e2eRoutes: true }).value,
    );
    await handleSession(
      "login",
      post("login", {}, header),
      config(api.fetch, { e2eRoutes: false }).value,
    );
    expect(api.seen[0]!.headers.get("X-Wasla-Client-IP")).toBe("203.0.113.7");
    expect(api.seen[1]!.headers.get("X-Wasla-Client-IP")).not.toBe("203.0.113.7");
  });
});

describe("register-login (SR-05)", () => {
  it("registers (202) then logs in; the cookie is set", async () => {
    const api = fakeApi({
      "/api/v1/auth/register": { status: 202, json: { status: "accepted" } },
      "/api/v1/auth/login": { status: 200, json: session },
    });
    const response = await handleSession(
      "register-login",
      post("register-login", {
        email: "n@example.com",
        password: "pw",
        full_name: "N",
        timezone: "Africa/Cairo",
      }),
      config(api.fetch).value,
    );
    expect(api.seen.map((s) => new URL(s.url).pathname)).toEqual([
      "/api/v1/auth/register",
      "/api/v1/auth/login",
    ]);
    expect(response.status).toBe(200);
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=/);
    expect(await bodyText(response)).not.toContain(REFRESH);
  });

  it("an address already taken (202, then a failing login) → check_inbox, no cookie", async () => {
    const api = fakeApi({
      "/api/v1/auth/register": { status: 202, json: { status: "accepted" } },
      "/api/v1/auth/login": { status: 401, json: { error: { code: "invalid_credentials" } } },
    });
    const response = await handleSession(
      "register-login",
      post("register-login", {}),
      config(api.fetch).value,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "check_inbox" });
    expect(response.headers.getSetCookie()).toEqual([]);
  });
});

describe("refresh (SR-05)", () => {
  it("no cookie → 401 unauthenticated without calling the API", async () => {
    const api = fakeApi({});
    const response = await handleSession("refresh", post("refresh", {}), config(api.fetch).value);
    expect(response.status).toBe(401);
    expect(api.seen).toEqual([]);
  });

  it("sends the cookie's token and the workspace_slug; rotates the cookie", async () => {
    const api = fakeApi({
      "/api/v1/auth/refresh": { status: 200, json: { ...session, refresh_token: "rt-2" } },
    });
    const response = await handleSession(
      "refresh",
      post("refresh", { workspace_slug: "acme" }, { Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(api.seen[0]!.body).toEqual({ refresh_token: REFRESH, workspace_slug: "acme" });
    expect(api.seen[0]!.headers.get("Cookie")).toBeNull();
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=rt-2; /);
    const text = await bodyText(response);
    expect(text).not.toContain("rt-2");
    expect(text).not.toContain(REFRESH);
  });

  it("a suspended workspace's refusal keeps the cookie and passes its code through (FEF-24)", async () => {
    const api = fakeApi({
      "/api/v1/auth/refresh": {
        status: 403,
        json: { error: { code: "workspace_suspended", message: "This workspace is suspended." } },
      },
    });
    const response = await handleSession(
      "refresh",
      post("refresh", { workspace_slug: "acme" }, { Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(response.status).toBe(403);
    expect(response.headers.getSetCookie()).toEqual([]);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "workspace_suspended",
    );
  });

  it("any other 403 on refresh still clears the cookie", async () => {
    const api = fakeApi({
      "/api/v1/auth/refresh": { status: 403, json: { error: { code: "permission_denied" } } },
    });
    const response = await handleSession(
      "refresh",
      post("refresh", {}, { Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(response.status).toBe(403);
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=; .*Max-Age=0/);
  });

  it("a refused refresh clears the cookie and passes the 401 through", async () => {
    const api = fakeApi({
      "/api/v1/auth/refresh": { status: 401, json: { error: { code: "invalid_token" } } },
    });
    const response = await handleSession(
      "refresh",
      post("refresh", {}, { Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(response.status).toBe(401);
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=; .*Max-Age=0/);
  });

  it("outside development the cookie is __Host-wasla_rt and Secure", async () => {
    const api = fakeApi({ "/api/v1/auth/refresh": { status: 200, json: session } });
    const response = await handleSession(
      "refresh",
      post(
        "refresh",
        {},
        { Cookie: `__Host-wasla_rt=${REFRESH}`, "CF-Connecting-IP": "198.51.100.4" },
      ),
      config(api.fetch, { environment: "preview" }).value,
    );
    expect(response.status).toBe(200);
    const cookie = response.headers.getSetCookie()[0]!;
    expect(cookie).toMatch(/^__Host-wasla_rt=/);
    expect(cookie).toContain("Secure");
    expect(api.seen[0]!.headers.get("X-Wasla-Client-IP")).toBe("198.51.100.4");
  });
});

describe("switch, logout, logout-all (SR-05)", () => {
  it("switch sends the bearer and the slug, returns the new access token, leaves the cookie", async () => {
    const api = fakeApi({
      "/api/v1/auth/workspace": {
        status: 200,
        json: { access_token: "access-2", expires_in: 900, active_workspace: { slug: "beta" } },
      },
    });
    const response = await handleSession(
      "switch",
      post("switch", { workspace_slug: "beta" }, { Authorization: "Bearer access-1" }),
      config(api.fetch).value,
    );
    expect(api.seen[0]!.headers.get("Authorization")).toBe("Bearer access-1");
    expect(api.seen[0]!.body).toEqual({ workspace_slug: "beta" });
    expect(response.headers.getSetCookie()).toEqual([]);
    expect(((await response.json()) as { access_token: string }).access_token).toBe("access-2");
  });

  it("logout clears the cookie even when the API no longer knows the token", async () => {
    const api = fakeApi({
      "/api/v1/auth/logout": { status: 401, json: { error: { code: "invalid_token" } } },
    });
    const response = await handleSession(
      "logout",
      post("logout", {}, { Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(response.status).toBe(200);
    expect(api.seen[0]!.body).toEqual({ refresh_token: REFRESH });
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=; .*Max-Age=0/);
  });

  it("logout-all sends the bearer and clears the cookie", async () => {
    const api = fakeApi({ "/api/v1/auth/logout-all": { status: 200, json: {} } });
    const response = await handleSession(
      "logout-all",
      post("logout-all", {}, { Authorization: "Bearer access-1", Cookie: `wasla_rt=${REFRESH}` }),
      config(api.fetch).value,
    );
    expect(api.seen[0]!.headers.get("Authorization")).toBe("Bearer access-1");
    expect(response.headers.getSetCookie()[0]).toMatch(/^wasla_rt=; .*Max-Age=0/);
  });
});

describe("Google through the relay (SR-07, FEB-21)", () => {
  it("authorize relays the plain-HTTP binding cookie under its own name, scoped to /session/google/", async () => {
    const api = fakeApi({
      "/api/v1/auth/google/authorize": {
        status: 200,
        json: { authorization_url: "http://127.0.0.1:8026/google/authorize?state=s" },
        setCookie: [
          "wasla_oauth=bind-1; HttpOnly; Path=/api/v1/auth/google; SameSite=Lax; Max-Age=600",
        ],
      },
    });
    const response = await handleSession(
      "google/authorize",
      post("google/authorize"),
      config(api.fetch).value,
    );
    expect(response.status).toBe(200);
    const cookie = response.headers.getSetCookie();
    expect(cookie).toHaveLength(1);
    expect(cookie[0]).toMatch(/^wasla_oauth=bind-1; /);
    expect(cookie[0]).toContain("Path=/session/google/");
    expect(await response.json()).toEqual({
      authorization_url: "http://127.0.0.1:8026/google/authorize?state=s",
    });
  });

  it("callback forwards code, state and the binding cookie, signs in, clears the binding", async () => {
    const api = fakeApi({ "/api/v1/auth/google/callback": { status: 200, json: session } });
    const response = await handleSession(
      "google/callback",
      post("google/callback", { code: "c", state: "s" }, { Cookie: "wasla_oauth=bind-1" }),
      config(api.fetch).value,
    );
    expect(api.seen[0]!.headers.get("Cookie")).toBe("wasla_oauth=bind-1");
    expect(api.seen[0]!.body).toEqual({ code: "c", state: "s" });
    const cookies = response.headers.getSetCookie();
    expect(cookies[0]).toMatch(/^wasla_rt=/);
    expect(cookies.some((c) => /^wasla_oauth=; .*Max-Age=0/.test(c))).toBe(true);
    expect(await bodyText(response)).not.toContain(REFRESH);
  });

  it("callback without the binding cookie: forwarded without one; the API's refusal passes by code", async () => {
    const api = fakeApi({
      "/api/v1/auth/google/callback": {
        status: 400,
        json: { error: { code: "oauth_state_invalid" } },
      },
    });
    const response = await handleSession(
      "google/callback",
      post("google/callback", { code: "c", state: "s" }),
      config(api.fetch).value,
    );
    expect(api.seen[0]!.headers.get("Cookie")).toBeNull();
    expect(response.status).toBe(400);
    expect(await bodyText(response)).toContain("oauth_state_invalid");
  });
});

describe("the handlers call the API origin only (SR-09)", () => {
  it("every API call of every verb goes to the API origin", async () => {
    const api = fakeApi({
      "/api/v1/auth/login": { status: 200, json: session },
      "/api/v1/auth/register": { status: 202, json: {} },
      "/api/v1/auth/refresh": { status: 200, json: session },
      "/api/v1/auth/workspace": { status: 200, json: session },
      "/api/v1/auth/logout": { status: 204 },
      "/api/v1/auth/logout-all": { status: 200, json: {} },
      "/api/v1/auth/google/authorize": {
        status: 200,
        json: { authorization_url: "http://127.0.0.1:8026/x" },
        setCookie: ["wasla_oauth=b; Path=/"],
      },
      "/api/v1/auth/google/callback": { status: 200, json: session },
    });
    for (const verb of SESSION_VERBS) {
      await handleSession(
        verb,
        post(
          verb,
          { workspace_slug: "acme", code: "c", state: "s" },
          {
            Cookie: `wasla_rt=${REFRESH}; wasla_oauth=b`,
            Authorization: "Bearer a",
          },
        ),
        config(api.fetch).value,
      );
    }
    expect(api.seen.length).toBeGreaterThanOrEqual(SESSION_VERBS.length);
    for (const call of api.seen) expect(new URL(call.url).origin).toBe(API);
  });
});
