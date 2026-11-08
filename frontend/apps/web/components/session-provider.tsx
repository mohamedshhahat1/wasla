"use client";

import { useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { unwrap, type components } from "@wasla/api-client";
import { SESSION_CHANNEL, type WorkspaceRef } from "@wasla/session";
import { Toast } from "@wasla/ui";

import { publicEnv } from "../lib/env";
import { WebSession, type SessionStatus } from "../lib/session/web-session";

export type Profile = components["schemas"]["ProfileResponse"];

interface SessionContextValue {
  session: WebSession;
  status: SessionStatus;
  workspace: WorkspaceRef | null;
  /** GET /auth/me for the token in hand; null until loaded or when signed out. */
  profile: Profile | null;
  profileLoaded: boolean;
  reloadProfile: () => Promise<Profile | null>;
  /** The holder's generation: part of every query key (PD-06). */
  generation: number;
}

const SessionContext = createContext<SessionContextValue | null>(null);

/** The auth pages: where a signed-out tab goes, and where a signed-in one never needs to. */
export const AUTH_PATHS = [
  "/login",
  "/signup",
  "/forgot",
  "/reset-password",
  "/auth/google/callback",
];

/** SB-01: one WebSession per tab, restored from the cookie on load, shared by every screen. */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [session] = useState(() => {
    const env = publicEnv();
    return new WebSession({
      apiOrigin: env.apiOrigin,
      locks: navigator.locks,
      channel: new BroadcastChannel(SESSION_CHANNEL),
    });
  });
  const queryClient = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const t = useTranslations("auth");
  const common = useTranslations("common");

  const snapshot = useSyncExternalStore(
    (listener) => session.subscribe(listener),
    () => `${session.status}|${session.holder.version}|${session.holder.generation}`,
    () => "loading|0|0",
  );
  const [status, version, generation] = snapshot.split("|") as [SessionStatus, string, string];
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [switchedNotice, setSwitchedNotice] = useState<string | null>(null);
  const lastWorkspace = useRef<string | null>(null);
  const wasSignedIn = useRef(false);

  useEffect(() => {
    void session.restore();
    return () => session.close();
  }, [session]);

  const reloadProfile = useCallback(async () => {
    if (!session.holder.state) {
      setProfile(null);
      setProfileLoaded(true);
      return null;
    }
    try {
      const me = await unwrap(session.api.GET("/api/v1/auth/me"));
      setProfile(me);
      return me;
    } catch {
      setProfile(null);
      return null;
    } finally {
      setProfileLoaded(true);
    }
  }, [session]);

  // A new generation (sign-in, switch, sign-out): nothing cached under the old one is read again.
  useEffect(() => {
    queryClient.clear();
    setProfileLoaded(false);
    void reloadProfile();
  }, [generation, queryClient, reloadProfile]);

  useEffect(() => {
    const slug = session.workspace?.slug ?? null;
    if (
      status === "signed-in" &&
      lastWorkspace.current &&
      slug &&
      slug !== lastWorkspace.current &&
      session.lastChangeFromAnotherTab
    ) {
      setSwitchedNotice(session.workspace?.name ?? slug);
    }
    lastWorkspace.current = slug;
  }, [version, status, session]);

  // Signed out here or in another tab: every tab goes to the log-in page (SB-01).
  useEffect(() => {
    if (status === "signed-in") wasSignedIn.current = true;
    if (status === "signed-out" && wasSignedIn.current) {
      wasSignedIn.current = false;
      queryClient.clear();
      setProfile(null);
      if (!AUTH_PATHS.includes(pathname)) router.replace("/login");
    }
  }, [status, pathname, queryClient, router]);

  const value = useMemo<SessionContextValue>(
    () => ({
      session,
      status,
      workspace: session.workspace,
      profile,
      profileLoaded,
      reloadProfile,
      generation: Number(generation),
    }),
    // version: a refresh changes the workspace object without changing the generation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session, status, version, generation, profile, profileLoaded, reloadProfile],
  );

  return (
    <SessionContext.Provider value={value}>
      {children}
      <Toast
        open={switchedNotice !== null}
        onOpenChange={(open) => {
          if (!open) setSwitchedNotice(null);
        }}
        title={t("switchedElsewhere", { workspace: switchedNotice ?? "" })}
        closeLabel={common("close")}
      />
    </SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}
