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
  status: "loading" | "signed-out" | "signed-in" | "connection-lost";
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

/** WK-03: unverified → /verify; no workspace → /workspaces/new; else the asked page (same origin). */
export function afterSignIn(
  state: Pick<RouteState, "verified" | "workspaceCount">,
  next?: unknown,
): string {
  if (!state.verified) return "/verify";
  if (!state.workspaceCount) return "/workspaces/new";
  return safeNext(next ?? "/");
}
