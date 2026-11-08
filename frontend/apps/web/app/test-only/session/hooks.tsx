"use client";

import { useEffect, useState } from "react";

import { openEventStream, type StreamFrameOut, type StreamState } from "@wasla/api-client/stream";
import { RefreshCoordinator, type LockManagerLike } from "@wasla/session";

import { useSession } from "../../../components/session-provider";
import { publicEnv } from "../../../lib/env";

/** The J6 control only: a lock manager that serialises nothing (M-S06's behaviour, on purpose). */
const noLock: LockManagerLike = { request: (_name, callback) => callback() };

interface Hooks {
  __waslaRefreshNow(): Promise<string>;
  __waslaRefreshNoLock(): Promise<string>;
  __waslaToken(): string | null;
  __waslaMe(): Promise<number>;
  __waslaStream(): void;
  __waslaStreamLog: Array<StreamFrameOut | StreamState>;
}

/** Puts the hooks on window for Playwright; renders the session status for the test to wait on. */
export function SessionHooks() {
  const { session, status } = useSession();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const target = window as unknown as Hooks;
    const unlocked = new RefreshCoordinator({
      holder: session.holder,
      locks: noLock,
      channel: session.channel,
    });
    target.__waslaRefreshNow = () => session.refresh();
    target.__waslaRefreshNoLock = () => session.refresh(unlocked);
    target.__waslaToken = () => session.holder.current(Date.now());
    target.__waslaMe = async () => (await session.api.GET("/api/v1/auth/me")).response.status;
    target.__waslaStreamLog = [];
    target.__waslaStream = () => {
      openEventStream({
        api: publicEnv().apiOrigin,
        getAccessToken: () => session.accessToken().catch(() => null),
        onUnauthenticated: () => session.refresh().catch(() => null),
        onFrame: (frame) => target.__waslaStreamLog.push(frame),
        onState: (state) => target.__waslaStreamLog.push(state),
      });
    };
    setReady(true);
  }, [session]);
  return (
    <p data-testid="session-hooks" data-ready={ready ? "yes" : "no"} data-status={status}>
      {status}
    </p>
  );
}
