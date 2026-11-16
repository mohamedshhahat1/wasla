// @vitest-environment jsdom
/**
 * FE-5's session rows (SE-01 .. SE-06, PR-01 .. PR-03, ADR-199):
 *   FEF-27  a refresh refused with 403 membership_revoked keeps the person signed in on GT-04's
 *           state ("Open another workspace" / "Sign out") - never "Connection lost";
 *   FEF-28  signed in without a workspace because every workspace is suspended → GT-03's state,
 *           never the shell and never the error page;
 *   FEF-26  only a preview build without an API says "this is a preview" for "Connection lost".
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";
import type { ChannelLike, LockManagerLike } from "@wasla/session";

import { PageGate } from "../components/page-gate";
import { decideRoute, onlySuspendedWorkspaces } from "../lib/routing";
import { WebSession } from "../lib/session/web-session";

const restore = vi.fn(() => Promise.resolve());
const refresh = vi.fn(() => Promise.resolve("token"));
const signOut = vi.fn(() => Promise.resolve());
const openAnotherWorkspace = vi.fn(() => Promise.resolve());
const reloadProfile = vi.fn(() => Promise.resolve(null));
let status = "membership-revoked";
let workspace: { id: string } | null = null;
let profile: { email_verified_at: string; workspaces: Array<{ status: string }> } | null = null;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({
    status,
    profile,
    profileLoaded: true,
    workspace,
    reloadProfile,
    session: { restore, refresh, signOut, openAnotherWorkspace },
  }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  for (const double of [restore, refresh, signOut, openAnotherWorkspace, reloadProfile]) {
    double.mockClear();
  }
  status = "membership-revoked";
  workspace = null;
  profile = null;
});

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
      {children}
    </NextIntlClientProvider>
  );
}

const en = messagesFor("en") as {
  app: {
    gates: { suspended: Record<string, string>; revoked: Record<string, string> };
    account: Record<string, string>;
  };
  auth: Record<string, string>;
};

const locks: LockManagerLike = { request: (_name, callback) => callback() };

function channel(): ChannelLike {
  return {
    postMessage: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    close: () => undefined,
  };
}

function sessionAnswering(answers: Array<{ status: number; body: unknown }>) {
  const bodies: unknown[] = [];
  const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(typeof init?.body === "string" ? init.body : "{}"));
    const next = answers.shift() ?? { status: 500, body: {} };
    return Promise.resolve(
      new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  const session = new WebSession({
    apiOrigin: "http://api.test",
    locks,
    channel: channel(),
    delay: () => Promise.resolve(),
    fetch,
  });
  return { session, fetch, bodies };
}

const REVOKED = { status: 403, body: { error: { code: "membership_revoked", message: "x" } } };

describe("the browser session reads a withdrawn membership (FEF-27, M-T38)", () => {
  it("403 membership_revoked → membership-revoked: signed in, not signed out", async () => {
    const { session, fetch } = sessionAnswering([REVOKED]);
    await session.restore();
    expect(session.status).toBe("membership-revoked");
    // Not retried: one refresh, one answer.
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('"Open another workspace" is one refresh that names no workspace', async () => {
    const grant = {
      access_token: "a",
      expires_in: 600,
      active_workspace: { id: "w2", slug: "other", name: "Other", role: "member" },
    };
    const { session, fetch, bodies } = sessionAnswering([REVOKED, { status: 200, body: grant }]);
    await session.restore();
    await session.openAnotherWorkspace();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(bodies[1]).toEqual({});
    expect(session.status).toBe("signed-in");
    expect(session.workspace?.slug).toBe("other");
  });

  it("a stranger's refusal still signs out (401 / 403 without the code)", async () => {
    for (const refusal of [
      { status: 401, body: { error: { code: "unauthenticated", message: "x" } } },
      { status: 403, body: { error: { code: "permission_denied", message: "x" } } },
    ]) {
      const { session } = sessionAnswering([refusal]);
      await session.restore();
      expect(session.status).toBe("signed-out");
    }
  });
});

describe("the page for a withdrawn membership (SE-02)", () => {
  it("routes to the page, never to the log-in page", () => {
    expect(
      decideRoute(
        "workspace",
        { status: "membership-revoked", verified: null, workspaceCount: null },
        "/contacts",
      ),
    ).toEqual({ kind: "show" });
  });

  it("shows GT-04's state with its two choices - not Connection lost, not the page", async () => {
    render(
      <PageGate need="workspace">
        <form aria-label="contacts" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("heading", { name: en.app.gates.revoked["title"]! })).toBeTruthy();
    expect(screen.getByText(en.app.gates.revoked["sessionBody"]!)).toBeTruthy();
    expect(screen.queryByText(en.auth["connectionLostTitle"]!)).toBeNull();
    expect(screen.queryByRole("form", { name: "contacts" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: en.app.gates.revoked["openAnother"]! }));
    await waitFor(() => expect(openAnotherWorkspace).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: en.app.account["signOut"]! }));
    expect(signOut).toHaveBeenCalledTimes(1);
    expect(restore).not.toHaveBeenCalled();
  });
});

describe("signed in with only suspended workspaces (FEF-28, M-T40)", () => {
  it("the rule: signed in, no workspace in hand, every workspace suspended", () => {
    const suspended = [{ status: "suspended" }];
    expect(onlySuspendedWorkspaces("signed-in", false, suspended)).toBe(true);
    expect(onlySuspendedWorkspaces("signed-in", true, suspended)).toBe(false);
    expect(onlySuspendedWorkspaces("signed-in", false, [])).toBe(false);
    expect(onlySuspendedWorkspaces("signed-in", false, null)).toBe(false);
    expect(onlySuspendedWorkspaces("signed-in", false, [...suspended, { status: "active" }])).toBe(
      false,
    );
    expect(onlySuspendedWorkspaces("loading", false, suspended)).toBe(false);
  });

  it("a workspace page shows GT-03's state; Check again asks for a session again", async () => {
    status = "signed-in";
    profile = { email_verified_at: "2026-11-01T00:00:00Z", workspaces: [{ status: "suspended" }] };
    render(
      <PageGate need="workspace">
        <form aria-label="home" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("heading", { name: en.app.gates.suspended["title"]! })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "home" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: en.app.gates.suspended["checkAgain"]! }));
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(reloadProfile).toHaveBeenCalledTimes(1));
  });

  it("with a usable workspace the page shows as before", () => {
    status = "signed-in";
    workspace = { id: "w1" };
    profile = { email_verified_at: "2026-11-01T00:00:00Z", workspaces: [{ status: "active" }] };
    render(
      <PageGate need="workspace">
        <form aria-label="home" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("form", { name: "home" })).toBeTruthy();
  });
});

describe("a preview without a server says so (FEF-26, M-T39)", () => {
  function connectionLost() {
    status = "connection-lost";
    render(
      <PageGate need="workspace">
        <form aria-label="home" />
      </PageGate>,
      { wrapper: Wrapper },
    );
  }

  it("every other build: Connection lost, exactly as before", () => {
    connectionLost();
    expect(screen.getByRole("heading", { name: en.auth["connectionLostTitle"]! })).toBeTruthy();
    expect(screen.queryByText(en.auth["previewTitle"]!)).toBeNull();
    expect(document.querySelector("[data-preview-notice]")).toBeNull();
  });

  it("a value other than the preview build's is not a preview", () => {
    vi.stubEnv("PUBLIC_PREVIEW_NO_API", "false");
    connectionLost();
    expect(document.querySelector("[data-preview-notice]")).toBeNull();
  });

  it("the preview build: the notice with the way to the sign-in page, and no retry", () => {
    vi.stubEnv("PUBLIC_PREVIEW_NO_API", "true");
    connectionLost();
    expect(screen.getByRole("heading", { name: en.auth["previewTitle"]! })).toBeTruthy();
    expect(screen.queryByText(en.auth["connectionLostTitle"]!)).toBeNull();
    const link = screen.getByRole("link", { name: en.auth["previewAction"]! });
    expect(link.getAttribute("href")).toBe("/login");
    expect(restore).not.toHaveBeenCalled();
  });
});
