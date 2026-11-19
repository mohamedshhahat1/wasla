/**
 * FE-7's session relays (SG-01 .. SG-04, ADR-201) against a fake API: connecting Google and
 * re-authenticating with it relay the binding cookie exactly as FE-2's sign-in does; the
 * re-authentication proof stays in a sealed HttpOnly cookie no page script can read, and the
 * account close spends it once. The generic refusals (origin, method, body size, missing secret)
 * run for these verbs in session-handlers.test.ts, which iterates every verb.
 */
import { describe, expect, it } from "vitest";

import {
  clearReauthCookieHeader,
  handleSession,
  openReauth,
  reauthCookieName,
  sealReauth,
  type SessionServerConfig,
  type SessionVerb,
} from "../lib/server/session-handlers";

const APP = "http://localhost:3000";
const API = "http://localhost:8000";
const SECRET = "test-forward-secret";
const PROOF = "reauth-proof-never-in-a-body";

type Reply = { status: number; json?: unknown; setCookie?: string[] };

function fakeApi(replies: Record<string, Reply>) {
  const seen: Array<{ url: string; method: string; headers: Headers; body: unknown }> = [];
  const fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = input instanceof Request ? input.url : input.toString();
    seen.push({
      url,
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
    });
    const reply = replies[new URL(url).pathname];
    if (!reply) return Promise.resolve(new Response("not faked", { status: 599 }));
    const headers = new Headers({ "Content-Type": "application/json", "X-Request-ID": "req" });
    for (const cookie of reply.setCookie ?? []) headers.append("Set-Cookie", cookie);
    return Promise.resolve(
      new Response(reply.json === undefined ? null : JSON.stringify(reply.json), {
        status: reply.status,
        headers,
      }),
    );
  };
  return { seen, fetch };
}

function config(fetch: typeof globalThis.fetch): SessionServerConfig {
  return {
    environment: "development",
    appOrigin: APP,
    apiOrigin: API,
    secret: SECRET,
    e2eRoutes: false,
    fetch,
    now: () => Date.UTC(2026, 10, 19, 10, 0, 0),
    log: () => undefined,
  };
}

function post(verb: SessionVerb, body: unknown = {}, headers: Record<string, string> = {}) {
  return new Request(`${APP}/session/${verb}`, {
    method: "POST",
    headers: {
      Origin: APP,
      "Sec-Fetch-Site": "same-origin",
      "X-Wasla-Session": "1",
      "Content-Type": "application/json",
      Authorization: "Bearer access-1",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

const AUTHORIZE = {
  status: 200,
  json: { authorization_url: "http://127.0.0.1:8026/google/authorize?state=s" },
  setCookie: ["wasla_oauth=bind-1; HttpOnly; Path=/api/v1/auth; SameSite=Lax; Max-Age=600"],
};

describe("connecting Google through the relay (SG-02)", () => {
  it("authorize: the bearer call, the binding relayed onto /session/google/", async () => {
    const api = fakeApi({ "/api/v1/auth/identities/google/authorize": AUTHORIZE });
    const response = await handleSession(
      "google/link/authorize",
      post("google/link/authorize"),
      config(api.fetch),
    );
    expect(response.status).toBe(200);
    expect(api.seen[0]!.headers.get("Authorization")).toBe("Bearer access-1");
    expect(api.seen[0]!.method).toBe("POST");
    const [cookie] = response.headers.getSetCookie();
    expect(cookie).toMatch(/^wasla_oauth=bind-1; Path=\/session\/google\/; /);
    expect(await response.json()).toEqual({
      authorization_url: "http://127.0.0.1:8026/google/authorize?state=s",
    });
  });

  it("callback: code, state, binding and bearer forwarded; answers only that it is connected", async () => {
    const api = fakeApi({
      "/api/v1/auth/identities/google/link": {
        status: 200,
        json: { provider: "google", email: "private@example.com", subject: "g-123" },
      },
    });
    const response = await handleSession(
      "google/link/callback",
      post("google/link/callback", { code: "c", state: "s" }, { Cookie: "wasla_oauth=bind-1" }),
      config(api.fetch),
    );
    expect(api.seen[0]!.body).toEqual({ code: "c", state: "s" });
    expect(api.seen[0]!.headers.get("Cookie")).toBe("wasla_oauth=bind-1");
    expect(await response.json()).toEqual({ connected: true });
    expect(response.headers.getSetCookie().some((c) => /^wasla_oauth=; .*Max-Age=0/.test(c))).toBe(
      true,
    );
  });

  it("a refusal passes by its code and still clears the binding", async () => {
    const api = fakeApi({
      "/api/v1/auth/identities/google/link": {
        status: 409,
        json: { error: { code: "google_identity_in_use", message: "x" } },
      },
    });
    const response = await handleSession(
      "google/link/callback",
      post("google/link/callback", { code: "c", state: "s" }),
      config(api.fetch),
    );
    expect(response.status).toBe(409);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "google_identity_in_use",
    );
    expect(response.headers.getSetCookie().some((c) => /Max-Age=0/.test(c))).toBe(true);
  });

  it("no code or state: 422 before any API call", async () => {
    const api = fakeApi({});
    const response = await handleSession(
      "google/link/callback",
      post("google/link/callback", { code: "c" }),
      config(api.fetch),
    );
    expect(response.status).toBe(422);
    expect(api.seen).toHaveLength(0);
  });
});

describe("re-authenticating with Google keeps the proof out of the page (SG-02, M-Q37)", () => {
  it("the callback seals the proof into an HttpOnly cookie on /session/account/; the body has none", async () => {
    const api = fakeApi({
      "/api/v1/auth/google/reauth/callback": {
        status: 200,
        json: { reauthentication_token: PROOF, expires_in: 900 },
      },
    });
    const response = await handleSession(
      "google/reauth/callback",
      post("google/reauth/callback", { code: "c", state: "s" }, { Cookie: "wasla_oauth=bind-1" }),
      config(api.fetch),
    );
    const text = await response.text();
    expect(text).not.toContain(PROOF);
    expect(JSON.parse(text)).toEqual({ ready: true, expires_in: 300 });
    const sealed = response.headers
      .getSetCookie()
      .find((c) => c.startsWith(`${reauthCookieName("development")}=`));
    expect(sealed).toBeDefined();
    expect(sealed).toContain("Path=/session/account/");
    expect(sealed).toContain("HttpOnly");
    expect(sealed).toContain("Max-Age=300");
    expect(sealed).not.toContain(PROOF);
    const value = /^wasla_reauth=([^;]+)/.exec(sealed!)![1]!;
    expect(openReauth(value, SECRET)).toBe(PROOF);
    expect(openReauth(value, "another-secret")).toBeNull();
  });

  it("account/close spends the sealed proof once with DELETE /auth/me and signs out", async () => {
    const api = fakeApi({ "/api/v1/auth/me": { status: 200, json: { id: "u1" } } });
    const response = await handleSession(
      "account/close",
      post("account/close", {}, { Cookie: `wasla_reauth=${sealReauth(PROOF, SECRET)}` }),
      config(api.fetch),
    );
    expect(response.status).toBe(200);
    expect(api.seen[0]!.method).toBe("DELETE");
    expect(api.seen[0]!.body).toEqual({ reauthentication_token: PROOF });
    expect(api.seen[0]!.headers.get("Authorization")).toBe("Bearer access-1");
    const cookies = response.headers.getSetCookie();
    expect(cookies).toContain(clearReauthCookieHeader("development"));
    expect(cookies.some((c) => /^wasla_rt=; /.test(c))).toBe(true);
    expect(await response.text()).not.toContain(PROOF);
  });

  it("account/close with no proof (or a forged one) is refused before the API, and clears it", async () => {
    for (const cookie of [undefined, "wasla_reauth=forged-value"]) {
      const api = fakeApi({});
      const response = await handleSession(
        "account/close",
        post("account/close", {}, cookie ? { Cookie: cookie } : {}),
        config(api.fetch),
      );
      expect(response.status).toBe(409);
      expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
        "reauthentication_required",
      );
      expect(api.seen).toHaveLength(0);
      expect(response.headers.getSetCookie()).toContain(clearReauthCookieHeader("development"));
    }
  });

  it("account_owns_workspaces passes with the workspaces (id, name, slug only)", async () => {
    const api = fakeApi({
      "/api/v1/auth/me": {
        status: 409,
        json: {
          error: {
            code: "account_owns_workspaces",
            message: "Transfer or delete...",
            details: { workspaces: [{ id: "w1", name: "Acme", slug: "acme", extra: "x" }] },
          },
        },
      },
    });
    const response = await handleSession(
      "account/close",
      post("account/close", {}, { Cookie: `wasla_reauth=${sealReauth(PROOF, SECRET)}` }),
      config(api.fetch),
    );
    expect(response.status).toBe(409);
    const body = (await response.json()) as {
      error: { code: string; details: { workspaces: unknown[] } };
    };
    expect(body.error.code).toBe("account_owns_workspaces");
    expect(body.error.details.workspaces).toEqual([{ id: "w1", name: "Acme", slug: "acme" }]);
    expect(response.headers.getSetCookie()).toContain(clearReauthCookieHeader("development"));
  });

  it("the sealed cookie is Secure and prefixed outside development", () => {
    expect(reauthCookieName("production")).toBe("__Secure-wasla_reauth");
    expect(clearReauthCookieHeader("preview")).toContain("; Secure");
  });
});
