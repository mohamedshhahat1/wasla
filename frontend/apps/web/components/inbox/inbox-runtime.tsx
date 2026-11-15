"use client";

/**
 * What the inbox keeps per tab while it is open (FE-4): the send queue (CP-02), the media registry
 * and its three-at-a-time gate (TH-04), the stream's two inbox reactions (RT-01, RT-03) and the
 * polling fallback (RT-04). Mounted inside the workspace's frame, so a switch or a sign-out
 * unmounts it - every object URL is revoked, every pending bubble and draft let go.
 */
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { unwrap, type components } from "@wasla/api-client";

import { messagesKey, unreadSummaryKey } from "../../lib/inbox/keys";
import { downSince, pollInterval, shouldPoll, StreamWatch } from "../../lib/inbox/live";
import { countingUrls, Gate, MediaRegistry, type CountingUrls } from "../../lib/inbox/media";
import { SendQueue } from "../../lib/inbox/send-queue";
import { refreshAfterGap, refreshInboxLists, registerInboxHooks } from "../../lib/stream-handlers";
import { appNow, hooksOn } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";

type MessageRead = components["schemas"]["MessageRead"];
type MessagePage = { items: MessageRead[]; next_cursor: string | null };

export interface InboxRuntime {
  queue: SendQueue;
  media: MediaRegistry;
  gate: Gate;
  /** The open view's poll interval, from the list's `poll_after_seconds`. */
  setPollAfter: (seconds: number | null) => void;
  /** The conversation on screen, so a poll and a gap know what to read. */
  setOpenConversation: (id: string | null) => void;
  /** Fetch the messages after the newest loaded one and append them (RT-01, RT-04). */
  catchUp: (conversationId: string) => Promise<void>;
}

const Context = createContext<InboxRuntime | null>(null);

export function useInboxRuntime(): InboxRuntime {
  const value = useContext(Context);
  if (!value) throw new Error("useInboxRuntime outside InboxRuntimeProvider");
  return value;
}

declare global {
  interface Window {
    /** IJ-05's counter of object URLs, in a hooks build only. */
    __waslaFe4?: { urls: () => { created: number; revoked: number }; registry: () => number };
  }
}

function urlApi(): CountingUrls {
  const base = {
    createObjectURL: (blob: Blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url: string) => URL.revokeObjectURL(url),
  };
  return countingUrls(base);
}

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

export function InboxRuntimeProvider({ children }: { children: ReactNode }) {
  const { session } = useSession();
  const { scope, liveState } = useWorkspace();
  const queryClient = useQueryClient();

  const [runtime] = useState(() => {
    const urls = urlApi();
    return {
      urls,
      queue: new SendQueue(),
      media: new MediaRegistry(urls),
      gate: new Gate(),
    };
  });
  const openRef = useRef<string | null>(null);
  const pollAfterRef = useRef<number | null>(null);

  const catchUp = useMemo(
    () => async (conversationId: string) => {
      const key = messagesKey(scope, conversationId);
      const data = queryClient.getQueryData<InfiniteData<MessagePage, string | null>>(key);
      const newest = data?.pages[0]?.items[0];
      if (!data || !newest) {
        await queryClient.invalidateQueries({ queryKey: key });
        return;
      }
      const page = await unwrap(
        session.api.GET("/api/v1/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId }, query: { after: newest.id } },
        }),
      );
      if (!page.items.length) return;
      // The after= page is oldest first; the first loaded page is newest first.
      const fresh = [...page.items].reverse();
      queryClient.setQueryData<InfiniteData<MessagePage, string | null>>(key, (current) => {
        if (!current) return current;
        const known = new Set(current.pages.flatMap((p) => p.items.map((item) => item.id)));
        const added = fresh.filter((item) => !known.has(item.id));
        if (!added.length) return current;
        const [first, ...rest] = current.pages;
        return { ...current, pages: [{ ...first!, items: [...added, ...first!.items] }, ...rest] };
      });
      runtime.queue.reconcile(page.items.map((item) => item.id));
    },
    [queryClient, scope, session, runtime.queue],
  );

  // RT-03: refetch from the top - the lists' first pages, the open conversation, its newest page.
  const gap = useMemo(
    () => () => refreshAfterGap(queryClient, scope, openRef.current),
    [queryClient, scope],
  );

  useEffect(() => registerInboxHooks({ catchUp: (id) => void catchUp(id), gap }), [catchUp, gap]);

  // A reconnect, or the first open after the inbox saw the stream closed, is a possible gap
  // (RT-03, FEF-37).
  const watch = useRef(new StreamWatch());
  const liveKind = useRef(liveState.kind);
  const down = useRef<number | null>(null);
  useEffect(() => {
    liveKind.current = liveState.kind;
    down.current = downSince(down.current, liveState.kind, appNow());
    if (watch.current.next(liveState.kind)) gap();
  }, [liveState, gap]);

  // RT-04: poll while the stream is down past its quiet period; never in a hidden tab. The timer is
  // not restarted by the stream's reconnect attempts - it reads the state when it fires.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      const since = down.current;
      if (
        since !== null &&
        shouldPoll({ stream: liveKind.current, since, now: appNow(), visible: visible() })
      ) {
        refreshInboxLists(queryClient, scope);
        // The unread total moves with the lists (FEF-37): no frame updates it while down.
        void queryClient.invalidateQueries({ queryKey: unreadSummaryKey(scope) });
        const open = openRef.current;
        if (open) void catchUp(open).catch(() => undefined);
      }
      timer = setTimeout(tick, pollInterval(pollAfterRef.current));
    };
    timer = setTimeout(tick, pollInterval(pollAfterRef.current));
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [queryClient, scope, catchUp]);

  // TH-04: every object URL goes on page hide and when the inbox unmounts (sign-out, switch).
  useEffect(() => {
    const { media, queue } = runtime;
    const onHide = () => media.revokeAll();
    window.addEventListener("pagehide", onHide);
    if (hooksOn()) {
      window.__waslaFe4 = {
        urls: () => ({ created: runtime.urls.created, revoked: runtime.urls.revoked }),
        registry: () => media.size,
      };
    }
    return () => {
      window.removeEventListener("pagehide", onHide);
      media.revokeAll();
      queue.clear();
    };
  }, [runtime]);

  const value = useMemo<InboxRuntime>(
    () => ({
      queue: runtime.queue,
      media: runtime.media,
      gate: runtime.gate,
      setPollAfter: (seconds) => {
        pollAfterRef.current = seconds;
      },
      setOpenConversation: (id) => {
        openRef.current = id;
      },
      catchUp,
    }),
    [runtime, catchUp],
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
}
