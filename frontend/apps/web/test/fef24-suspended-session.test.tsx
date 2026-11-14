// @vitest-environment jsdom
/**
 * FEF-24: a reload of a suspended workspace keeps the person signed in. The refresh route keeps
 * the cookie for 403 workspace_suspended; the browser session reads it as "workspace-suspended"
 * (not "signed-out"); PageGate draws GT-03's state with "Check again" and "Sign out".
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";
import type { ChannelLike, LockManagerLike } from "@wasla/session";

import { PageGate } from "../components/page-gate";
import { decideRoute } from "../lib/routing";
import { WebSession } from "../lib/session/web-session";

const restore = vi.fn(() => Promise.resolve());
const signOut = vi.fn(() => Promise.resolve());
let status = "workspace-suspended";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({
    status,
    profile: null,
    profileLoaded: true,
    session: { restore, signOut },
  }),
}));

afterEach(() => {
  cleanup();
  restore.mockClear();
  signOut.mockClear();
  status = "workspace-suspended";
});

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
      {children}
    </NextIntlClientProvider>
  );
}

const app = messagesFor("en")["app"] as {
  gates: { suspended: Record<string, string> };
  account: Record<string, string>;
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

function sessionAnswering(status: number, code: string | null): WebSession {
  const fetch = vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify(code ? { error: { code, message: code } } : {}), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  return new WebSession({
    apiOrigin: "http://api.test",
    locks,
    channel: channel(),
    delay: () => Promise.resolve(),
    fetch,
  });
}

describe("the browser session reads a refused refresh (SU-04)", () => {
  it("403 workspace_suspended on load → workspace-suspended, not signed out", async () => {
    const session = sessionAnswering(403, "workspace_suspended");
    await session.restore();
    expect(session.status).toBe("workspace-suspended");
    expect(session.holder.state).toBeNull();
  });

  it("401 on load → signed out, as before", async () => {
    const session = sessionAnswering(401, "unauthenticated");
    await session.restore();
    expect(session.status).toBe("signed-out");
  });

  it("403 membership_revoked on load → signed out, as before", async () => {
    const session = sessionAnswering(403, "membership_revoked");
    await session.restore();
    expect(session.status).toBe("signed-out");
  });
});

describe("routing and the page for a suspended session (FEF-24)", () => {
  it("routes to the page, never to the log-in page", () => {
    expect(
      decideRoute(
        "workspace",
        { status: "workspace-suspended", verified: null, workspaceCount: null },
        "/",
      ),
    ).toEqual({ kind: "show" });
  });

  it("a workspace page shows GT-03's state with Check again and Sign out", () => {
    render(
      <PageGate need="workspace">
        <form aria-label="home" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("heading", { name: app.gates.suspended["title"]! })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "home" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: app.gates.suspended["checkAgain"]! }));
    expect(restore).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: app.account["signOut"]! }));
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it("an auth page keeps its form", () => {
    render(
      <PageGate need="signed-out">
        <form aria-label="log in" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("form", { name: "log in" })).toBeTruthy();
  });
});
