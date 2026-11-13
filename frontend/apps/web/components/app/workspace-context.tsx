"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { queryKey, unwrap, type QueryScope, type WaslaClient } from "@wasla/api-client";
import { openEventStream, type StreamFrameOut } from "@wasla/api-client/stream";

import { publicEnv } from "../../lib/env";
import {
  decideGates,
  GATE_SIGNAL_CODES,
  type AllowanceFacts,
  type BannerKey,
  type GateDecision,
  type LiveCondition,
  type WorkspaceGatesFacts,
} from "../../lib/gates";
import type { TenantRole } from "../../lib/navigation";
import {
  StreamCoordinator,
  type SharedState,
  type StreamLocks,
} from "../../lib/stream-coordinator";
import { handleFrame } from "../../lib/stream-handlers";
import { appNow, bypassStreamLock, installHooks } from "../../lib/test-hooks";
import { useSession } from "../session-provider";

type Middleware = Parameters<WaslaClient["use"]>[0];

/** GT-02: a tab hidden this long re-reads the gates when it is shown again. */
export const HIDDEN_RECHECK_MS = 5 * 60_000;
/** GT-08: the stream may reconnect quietly this long before the banner says so. */
export const LIVE_QUIET_MS = 10_000;

export interface WorkspaceContextValue {
  workspaceId: string;
  scope: QueryScope;
  role: TenantRole | null;
  workspaceName: string;
  decision: GateDecision;
  gates: WorkspaceGatesFacts | null;
  allowance: AllowanceFacts | null;
  liveState: SharedState;
  /** Re-read the gates, the entitlements and the profile (a gate signal, "Check again"). */
  recheck: () => Promise<void>;
  dismiss: (key: BannerKey) => void;
  reconnectStream: () => void;
  /** ST-05: closes this workspace's stream; the switcher calls it before the new token. */
  leaveWorkspace: () => void;
  /** After a switch that failed: this workspace's stream again. */
  rejoinWorkspace: () => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace outside WorkspaceProvider");
  return value;
}

function locksOf(): StreamLocks | null {
  if (typeof navigator === "undefined" || !navigator.locks) return null;
  if (bypassStreamLock()) {
    // JR-09's control only: every tab "holds" the lock at once.
    return { request: (_name, _options, callback) => callback() };
  }
  return navigator.locks;
}

function codeOf(body: unknown): string | null {
  const code = (body as { error?: { code?: unknown } } | null)?.error?.code;
  return typeof code === "string" ? code : null;
}

/** The live condition a banner needs, from the shared stream state and how long it has lasted. */
function liveCondition(state: SharedState, since: number, now: number): LiveCondition {
  switch (state.kind) {
    case "open":
    case "stopped":
      return "ok";
    case "connecting":
    case "waiting":
      return now - since >= LIVE_QUIET_MS ? "retrying" : "ok";
    case "ended":
      if (state.reason === "workspace_switched") return "opened-elsewhere";
      // The gates draw these two; the banner would only repeat them.
      if (state.reason === "workspace_unavailable" || state.reason === "membership_revoked") {
        return "ok";
      }
      return "stopped";
  }
}

/**
 * Everything the signed-in shell knows about the active workspace (FE-3): its gates, read once
 * on entering it and again only on a signal (GT-02); the AI allowance; the error codes any call
 * of the session's client receives (one middleware, GT-01); and the one stream per browser (ST).
 * Mounted per workspace and session generation, so a switch starts from nothing.
 */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { session, profile, workspace, generation, reloadProfile } = useSession();
  const queryClient = useQueryClient();
  const workspaceId = workspace?.id ?? "";
  const summary = profile?.workspaces?.find((item) => item.id === workspaceId) ?? null;
  const role = ((summary?.role ?? workspace?.role ?? null) as TenantRole | null) ?? null;
  const scope = useMemo<QueryScope>(
    () => ({ workspaceId, sessionGeneration: generation }),
    [workspaceId, generation],
  );

  const [suspendedSignal, setSuspendedSignal] = useState(false);
  const [revokedSignal, setRevokedSignal] = useState(false);
  const [dismissed, setDismissed] = useState<ReadonlySet<BannerKey>>(new Set());
  const [liveState, setLiveState] = useState<SharedState>({ kind: "connecting" });
  const [liveSince, setLiveSince] = useState(() => appNow());
  const [now, setNow] = useState(() => appNow());
  const coordinator = useRef<StreamCoordinator | null>(null);

  const gatesKey = useMemo(() => queryKey(scope, "workspace-gates"), [scope]);
  const entitlementsKey = useMemo(() => queryKey(scope, "entitlements"), [scope]);

  const gatesQuery = useQuery({
    queryKey: gatesKey,
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace/gates")),
    enabled: Boolean(workspaceId) && !revokedSignal,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const gates = (gatesQuery.data as WorkspaceGatesFacts | undefined) ?? null;
  const fullyGated = revokedSignal || suspendedSignal || gates?.status === "suspended";

  const entitlementsQuery = useQuery({
    queryKey: entitlementsKey,
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/entitlements")),
    enabled: Boolean(workspaceId) && !fullyGated,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const allowance = useMemo<AllowanceFacts | null>(() => {
    const row = entitlementsQuery.data?.find((entry) => entry.key === "period_ai_turns");
    return row ? { limit: row.limit, remaining: row.remaining, over_limit: row.over_limit } : null;
  }, [entitlementsQuery.data]);

  const rechecking = useRef(false);
  const recheck = useCallback(async () => {
    if (rechecking.current) return;
    rechecking.current = true;
    try {
      await Promise.all([
        reloadProfile(),
        queryClient.invalidateQueries({ queryKey: gatesKey, exact: true }),
        queryClient.invalidateQueries({ queryKey: entitlementsKey, exact: true }),
      ]);
    } finally {
      rechecking.current = false;
    }
  }, [queryClient, gatesKey, entitlementsKey, reloadProfile]);

  // A gate read that says "active" again ends a suspension signal (GT-03's restore).
  useEffect(() => {
    if (gates?.status === "active" && summary?.status !== "suspended") setSuspendedSignal(false);
  }, [gates, summary?.status, gatesQuery.dataUpdatedAt]);

  // GT-01: one place turns the codes on any call of the session's client into gate signals.
  useEffect(() => {
    const middleware: Middleware = {
      async onResponse({ response }) {
        if (response.ok || response.status < 400) return response;
        const code = codeOf(
          await response
            .clone()
            .json()
            .catch(() => null),
        );
        if (!code || !GATE_SIGNAL_CODES.has(code)) return response;
        if (code === "membership_revoked") setRevokedSignal(true);
        if (code === "workspace_suspended") setSuspendedSignal(true);
        if (code !== "membership_revoked") void recheck();
        else void reloadProfile();
        return response;
      },
    };
    session.api.use(middleware);
    return () => session.api.eject(middleware);
  }, [session, recheck, reloadProfile]);

  // GT-02: a tab shown again after five minutes hidden re-reads the gates; never on every focus.
  useEffect(() => {
    let hiddenAt: number | null = null;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") hiddenAt = appNow();
      else if (hiddenAt !== null) {
        if (appNow() - hiddenAt >= HIDDEN_RECHECK_MS) void recheck();
        hiddenAt = null;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [recheck]);

  // ST-01..ST-05: the stream while signed in to an active, permitted workspace.
  const streamAllowed = Boolean(workspaceId) && !fullyGated;
  useEffect(() => {
    if (!streamAllowed) return;
    const locks = locksOf();
    if (!locks || typeof BroadcastChannel === "undefined") return;
    const api = publicEnv().apiOrigin;
    const next = new StreamCoordinator(workspaceId, {
      locks,
      channel: (name) => new BroadcastChannel(name),
      open: (handlers, lastEventId) =>
        openEventStream({
          api,
          getAccessToken: () => session.accessToken().catch(() => null),
          onUnauthenticated: () => session.refresh().catch(() => null),
          onFrame: handlers.onFrame,
          onState: handlers.onState,
          // A new EventSource cannot send Last-Event-ID; the route accepts the query alias.
          eventSource: (url) =>
            new EventSource(
              lastEventId ? `${url}&lastEventId=${encodeURIComponent(lastEventId)}` : url,
            ),
        }),
    });
    coordinator.current = next;
    const stopFrames = next.onFrame((frame: StreamFrameOut) => {
      handleFrame(frame, { queryClient, scope });
      if (frame.type === "closed") {
        const reason = (frame.data as { reason?: unknown } | null)?.reason;
        if (reason === "workspace_unavailable") {
          setSuspendedSignal(true);
          void recheck();
        }
        if (reason === "membership_revoked") {
          setRevokedSignal(true);
          void reloadProfile();
        }
      }
    });
    const stopState = next.onState((state) => {
      setLiveState(state);
      setLiveSince(appNow());
    });
    next.start();
    installHooks(queryClient, () => ({ role: next.role, state: next.state.kind }));
    return () => {
      stopFrames();
      stopState();
      next.stop();
      if (coordinator.current === next) coordinator.current = null;
    };
  }, [streamAllowed, workspaceId, session, queryClient, scope, recheck, reloadProfile]);

  // GT-08's quiet period: re-render once it has passed, so "retrying" can show.
  useEffect(() => {
    if (liveState.kind !== "connecting" && liveState.kind !== "waiting") return;
    const timer = setTimeout(() => setNow(appNow()), LIVE_QUIET_MS + 50);
    return () => clearTimeout(timer);
  }, [liveState, liveSince]);

  const decision = useMemo(
    () =>
      decideGates({
        role,
        profileStatus: summary?.status ?? null,
        suspendedSignal,
        revokedSignal,
        gates,
        allowance,
        live: liveCondition(liveState, liveSince, Math.max(now, appNow())),
        now: Math.max(now, appNow()),
        dismissed,
      }),
    [
      role,
      summary?.status,
      suspendedSignal,
      revokedSignal,
      gates,
      allowance,
      liveState,
      liveSince,
      now,
      dismissed,
    ],
  );

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspaceId,
      scope,
      role,
      workspaceName: summary?.name ?? workspace?.name ?? "",
      decision,
      gates,
      allowance,
      liveState,
      recheck,
      dismiss: (key) => setDismissed((current) => new Set([...current, key])),
      reconnectStream: () => coordinator.current?.reconnect(),
      leaveWorkspace: () => coordinator.current?.stop(),
      rejoinWorkspace: () => coordinator.current?.start(),
    }),
    [
      workspaceId,
      scope,
      role,
      summary?.name,
      workspace?.name,
      decision,
      gates,
      allowance,
      liveState,
      recheck,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}
