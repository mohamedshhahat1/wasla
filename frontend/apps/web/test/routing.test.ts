/** AU-03 and WK-03: routing by session state, and ?next= kept to this app's own paths. */
import { describe, expect, it } from "vitest";

import { afterSignIn, decideRoute, type RouteState } from "../lib/routing";
import { safeNext } from "../lib/safe-next";

describe("?next= is honoured only for a same-origin path (open-redirect proof)", () => {
  const refused = [
    "//evil.example",
    "//evil.example/inbox",
    "/\\evil.example",
    "/\\/evil.example",
    "\\\\evil.example",
    "https://evil.example/",
    "http://localhost:3000/inbox",
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "data:text/html,x",
    "/\t/evil.example",
    "/\n/evil.example",
    " /inbox",
    "inbox",
    "",
    "/%0a/evil",
  ];
  for (const value of refused) {
    it(`refuses ${JSON.stringify(value)}`, () => {
      const result = safeNext(value);
      expect(result.startsWith("/")).toBe(true);
      expect(result.startsWith("//")).toBe(false);
      expect(new URL(result, "http://localhost:3000").origin).toBe("http://localhost:3000");
      if (value !== "/%0a/evil") expect(result).toBe("/");
    });
  }

  it("keeps a path with its query and fragment", () => {
    expect(safeNext("/workspaces?x=1#top")).toBe("/workspaces?x=1#top");
    expect(safeNext(undefined)).toBe("/");
    expect(safeNext(42)).toBe("/");
  });
});

const signedIn = (verified: boolean, workspaceCount: number): RouteState => ({
  status: "signed-in",
  verified,
  workspaceCount,
});

describe("decideRoute (AU-03)", () => {
  it("waits while the session or the profile loads", () => {
    expect(
      decideRoute("workspace", { status: "loading", verified: null, workspaceCount: null }, "/"),
    ).toEqual({
      kind: "wait",
    });
    expect(
      decideRoute("workspace", { status: "signed-in", verified: null, workspaceCount: null }, "/"),
    ).toEqual({ kind: "wait" });
  });

  it("signed out: the auth pages show; any other page goes to /login with a safe next", () => {
    const out: RouteState = { status: "signed-out", verified: null, workspaceCount: null };
    expect(decideRoute("signed-out", out, "/login")).toEqual({ kind: "show" });
    expect(decideRoute("workspace", out, "/workspaces")).toEqual({
      kind: "go",
      to: "/login?next=%2Fworkspaces",
    });
    expect(decideRoute("workspace", out, "//evil.example")).toEqual({
      kind: "go",
      to: "/login?next=%2F",
    });
  });

  it("unverified: every signed-in page but /verify goes to /verify", () => {
    expect(decideRoute("workspace", signedIn(false, 1), "/")).toEqual({
      kind: "go",
      to: "/verify",
    });
    expect(decideRoute("verified", signedIn(false, 0), "/workspaces/new")).toEqual({
      kind: "go",
      to: "/verify",
    });
    expect(decideRoute("signed-in", signedIn(false, 0), "/verify")).toEqual({ kind: "show" });
  });

  it("verified without a workspace → /workspaces/new; with one, the page shows", () => {
    expect(decideRoute("workspace", signedIn(true, 0), "/")).toEqual({
      kind: "go",
      to: "/workspaces/new",
    });
    expect(decideRoute("verified", signedIn(true, 0), "/workspaces/new")).toEqual({ kind: "show" });
    expect(decideRoute("workspace", signedIn(true, 2), "/")).toEqual({ kind: "show" });
  });

  it("an auth page visited while signed in goes on (WK-03), never off-origin", () => {
    expect(decideRoute("signed-out", signedIn(true, 1), "/login", "/workspaces")).toEqual({
      kind: "go",
      to: "/workspaces",
    });
    expect(decideRoute("signed-out", signedIn(true, 1), "/login", "//evil.example")).toEqual({
      kind: "go",
      to: "/",
    });
    expect(afterSignIn({ verified: false, workspaceCount: 3 }, "/x")).toBe("/verify");
    expect(afterSignIn({ verified: true, workspaceCount: 0 }, "/x")).toBe("/workspaces/new");
  });

  it("connection lost shows the page (its retry), never a sign-out", () => {
    expect(
      decideRoute(
        "workspace",
        { status: "connection-lost", verified: true, workspaceCount: 1 },
        "/",
      ),
    ).toEqual({ kind: "show" });
  });
});
