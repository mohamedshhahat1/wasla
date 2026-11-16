/**
 * AU-03: where a page sends the visitor, decided in the browser from the session state (never
 * in proxy.ts, PD-02). Pure, so every branch is unit-tested.
 */
import { safeNext } from "./safe-next";

/** What a page needs before it shows itself. */
export type PageNeed =
  /** The auth pages: a signed-in visitor goes on to where they were going. */
  | "signed-out"
  /** /verify: signed in, any verification state. */
  | "signed-in"
  /** /workspaces/new: signed in and verified. */
  | "verified"
  /** Every other page: verified and in a workspace. */
  | "workspace";

export interface RouteState {
  status:
    | "loading"
    | "signed-out"
    | "signed-in"
    | "connection-lost"
    | "workspace-suspended"
    | "membership-revoked";
  /** null while /auth/me is loading. */
  verified: boolean | null;
  workspaceCount: number | null;
}

export type RouteDecision = { kind: "wait" } | { kind: "show" } | { kind: "go"; to: string };

export function decideRoute(
  need: PageNeed,
  state: RouteState,
  here: string,
  next?: unknown,
): RouteDecision {
  if (state.status === "loading") return { kind: "wait" };
  if (state.status === "connection-lost") return { kind: "show" };
  // FEF-24: signed in on a suspended workspace - PageGate shows its state; an auth page shows.
  if (state.status === "workspace-suspended") return { kind: "show" };
  // FEF-27: signed in, the membership of that workspace withdrawn - PageGate shows its state.
  if (state.status === "membership-revoked") return { kind: "show" };
  if (state.status === "signed-out") {
    if (need === "signed-out") return { kind: "show" };
    return { kind: "go", to: `/login?next=${encodeURIComponent(safeNext(here))}` };
  }
  if (state.verified === null || state.workspaceCount === null) return { kind: "wait" };
  if (need === "signed-out") return { kind: "go", to: afterSignIn(state, next) };
  if (!state.verified)
    return need === "signed-in" ? { kind: "show" } : { kind: "go", to: "/verify" };
  if (need === "workspace" && state.workspaceCount === 0)
    return { kind: "go", to: "/workspaces/new" };
  return { kind: "show" };
}

/**
 * FEF-28: a sign-in that names no workspace opens a session without one when every workspace the
 * person belongs to is suspended (the API never opens a suspended workspace). That is GT-03's
 * suspended state - not the shell, whose every read would fail. False while anything is unknown,
 * for a person with no workspace at all, and as soon as one workspace is usable.
 */
export function onlySuspendedWorkspaces(
  status: RouteState["status"],
  hasActiveWorkspace: boolean,
  workspaces: ReadonlyArray<{ status?: string | null }> | null | undefined,
): boolean {
  if (status !== "signed-in" || hasActiveWorkspace) return false;
  if (!workspaces || workspaces.length === 0) return false;
  return workspaces.every((workspace) => workspace.status === "suspended");
}

/** WK-03: unverified → /verify; no workspace → /workspaces/new; else the asked page (same origin). */
export function afterSignIn(
  state: Pick<RouteState, "verified" | "workspaceCount">,
  next?: unknown,
): string {
  if (!state.verified) return "/verify";
  if (!state.workspaceCount) return "/workspaces/new";
  return safeNext(next ?? "/");
}
