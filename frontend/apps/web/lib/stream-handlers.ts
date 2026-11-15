/**
 * What each stream frame does (FE-4, RT-01; FE-3's ST-04 table, replaced by the inbox's map). Events
 * invalidate or patch queries, never carry data (plan section 10): a frame names what changed, and
 * the screens read it again. The table is typed by the event vocabulary (`EVENT_TYPES`, held equal
 * to FE-0's event-types.json), so a new event type that is not handled fails to compile.
 *
 * Each tab applies a frame once (the coordinator's fan-out) and refetches only what it has loaded:
 * an invalidation of a query no screen holds costs nothing. Bursts are coalesced per key (RT-02).
 * FE-3's coordinator and the workspace context are unchanged: the open inbox registers the two
 * reactions that need more than an invalidation (`registerInboxHooks`).
 */
import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import {
  EVENT_TYPES,
  queryKey,
  type EventType,
  type QueryScope,
  type StreamFrame,
} from "@wasla/api-client";
import type { StreamFrameOut } from "@wasla/api-client/stream";

import { conversationKey, inboxPrefix, LEAD, messagesKey, unreadSummaryKey } from "./inbox/keys";

export interface InboxFrameHooks {
  /** message.created for a conversation whose messages are loaded: fetch the after= page. */
  catchUp?: (conversationId: string) => void;
  /** resync, or a reconnect without replay: refetch from the top (RT-03). */
  gap?: () => void;
}

export interface FrameContext {
  queryClient: QueryClient;
  scope: QueryScope;
  /** Defaults to the hooks the open inbox registered. */
  inbox?: InboxFrameHooks;
}

let registered: InboxFrameHooks | null = null;

/** The open inbox's reactions; the returned function removes them. */
export function registerInboxHooks(hooks: InboxFrameHooks): () => void {
  registered = hooks;
  return () => {
    if (registered === hooks) registered = null;
  };
}

/** home's conversations card (HM-02). */
export const HOME_CONVERSATIONS = "home-conversations";

/** RT-02: invalidations of one key within this window become one refetch. */
export const COALESCE_MS = 250;

const noop = () => undefined;

type Timer = ReturnType<typeof setTimeout>;

/** One coalescer per tab: a burst of frames for one key is one invalidation. */
export class Coalescer {
  #pending = new Map<string, { timer: Timer; run: () => void }>();
  #schedule: (run: () => void, ms: number) => Timer;
  #cancel: (timer: Timer) => void;

  constructor(
    schedule: (run: () => void, ms: number) => Timer = (run, ms) => setTimeout(run, ms),
    cancel: (timer: Timer) => void = (timer) => clearTimeout(timer),
  ) {
    this.#schedule = schedule;
    this.#cancel = cancel;
  }

  add(key: string, run: () => void): void {
    if (this.#pending.has(key)) return;
    const timer = this.#schedule(() => {
      this.#pending.delete(key);
      run();
    }, COALESCE_MS);
    this.#pending.set(key, { timer, run });
  }

  flush(): void {
    for (const [key, entry] of [...this.#pending]) {
      this.#cancel(entry.timer);
      this.#pending.delete(key);
      entry.run();
    }
  }

  clear(): void {
    for (const entry of this.#pending.values()) this.#cancel(entry.timer);
    this.#pending.clear();
  }
}

let coalescer = new Coalescer();

/** For the unit tests: a coalescer with an injected timer. */
export function setCoalescer(next: Coalescer): void {
  coalescer.clear();
  coalescer = next;
}

function dataOf(frame: StreamFrameOut): Record<string, unknown> {
  return frame.data && typeof frame.data === "object"
    ? (frame.data as Record<string, unknown>)
    : {};
}

function idOf(frame: StreamFrameOut, field: string): string | null {
  const value = dataOf(frame)[field];
  return typeof value === "string" ? value : null;
}

function fieldsOf(frame: StreamFrameOut): string[] {
  const value = dataOf(frame).fields;
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function scopeKey(scope: QueryScope, name: string): string {
  return `${scope.workspaceId}:${scope.sessionGeneration}:${name}`;
}

/**
 * Every loaded view's first page is fetched again; the pages beyond it are dropped, so a refetch
 * never re-walks cursors (IL-06 decides what the person sees).
 */
export function refreshInboxLists(queryClient: QueryClient, scope: QueryScope): void {
  const prefix = inboxPrefix(scope);
  for (const query of queryClient.getQueryCache().findAll({ queryKey: prefix })) {
    queryClient.setQueryData<InfiniteData<unknown, unknown>>(query.queryKey, (data) =>
      data && data.pages.length > 1
        ? { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) }
        : data,
    );
  }
  void queryClient.invalidateQueries({ queryKey: prefix });
}

/**
 * RT-03: after a gap (a reconnect, or the first open after the inbox saw the stream closed -
 * FEF-37) everything a frame would have moved is read again from the top: the lists' first pages,
 * the unread total (FEF-37: the nav count and "Unread only" read it), the open conversation and
 * its newest page, and the lead panel.
 */
export function refreshAfterGap(
  queryClient: QueryClient,
  scope: QueryScope,
  openConversationId: string | null,
): void {
  refreshInboxLists(queryClient, scope);
  void queryClient.invalidateQueries({ queryKey: unreadSummaryKey(scope) });
  if (openConversationId) {
    void queryClient.invalidateQueries({ queryKey: conversationKey(scope, openConversationId) });
    queryClient.setQueryData<InfiniteData<unknown, unknown>>(
      messagesKey(scope, openConversationId),
      (data) =>
        data && data.pages.length > 1
          ? { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) }
          : data,
    );
    void queryClient.invalidateQueries({ queryKey: messagesKey(scope, openConversationId) });
  }
  void queryClient.invalidateQueries({
    queryKey: [scope.workspaceId, scope.sessionGeneration, LEAD],
  });
}

function invalidateLists({ queryClient, scope }: FrameContext): void {
  coalescer.add(scopeKey(scope, "inbox"), () => refreshInboxLists(queryClient, scope));
}

function invalidateSummary({ queryClient, scope }: FrameContext): void {
  coalescer.add(scopeKey(scope, "summary"), () => {
    void queryClient.invalidateQueries({ queryKey: unreadSummaryKey(scope) });
  });
}

function invalidateConversation({ queryClient, scope }: FrameContext, id: string): void {
  coalescer.add(scopeKey(scope, `conversation:${id}`), () => {
    void queryClient.invalidateQueries({ queryKey: conversationKey(scope, id) });
  });
}

interface MessagePage {
  items: { id: string; status: string }[];
  next_cursor: string | null;
}

/** message.status: patch the loaded message's status in place - no request (RT-01). */
export function patchStatus(
  { queryClient, scope }: FrameContext,
  conversationId: string,
  messageId: string,
  status: string,
): boolean {
  let patched = false;
  queryClient.setQueryData<InfiniteData<MessagePage, unknown>>(
    messagesKey(scope, conversationId),
    (data) => {
      if (!data) return data;
      return {
        ...data,
        pages: data.pages.map((page) => ({
          ...page,
          items: page.items.map((item) => {
            if (item.id !== messageId || item.status === status) return item;
            patched = true;
            return { ...item, status };
          }),
        })),
      };
    },
  );
  return patched;
}

export const FRAME_HANDLERS: Record<
  EventType | StreamFrame,
  (frame: StreamFrameOut, context: FrameContext) => void
> = {
  "message.created": (frame, context) => {
    // FE-3's home card (HM-02), unchanged and at once.
    void context.queryClient.invalidateQueries({
      queryKey: queryKey(context.scope, HOME_CONVERSATIONS),
    });
    invalidateLists(context);
    invalidateSummary(context);
    const conversationId = idOf(frame, "conversation_id");
    if (!conversationId) return;
    invalidateConversation(context, conversationId);
    if (context.queryClient.getQueryData(messagesKey(context.scope, conversationId))) {
      const hooks = context.inbox ?? registered;
      coalescer.add(scopeKey(context.scope, `after:${conversationId}`), () =>
        hooks?.catchUp?.(conversationId),
      );
    }
  },
  "message.status": (frame, context) => {
    const conversationId = idOf(frame, "conversation_id");
    const messageId = idOf(frame, "message_id");
    const status = idOf(frame, "status");
    if (conversationId && messageId && status) {
      patchStatus(context, conversationId, messageId, status);
    }
  },
  "conversation.updated": (frame, context) => {
    const conversationId = idOf(frame, "conversation_id");
    if (conversationId) invalidateConversation(context, conversationId);
    invalidateLists(context);
    const fields = fieldsOf(frame);
    if (fields.includes("read") || fields.includes("status")) invalidateSummary(context);
    if (fields.includes("assignee") || fields.includes("owner")) {
      // A lead may have been attached with the change: the panel reads it again.
      void context.queryClient.invalidateQueries({
        queryKey: [context.scope.workspaceId, context.scope.sessionGeneration, LEAD],
      });
    }
  },
  "follow_up.updated": noop,
  "campaign.changed": noop,
  // A gap: the inbox reads its loaded lists, the open conversation and the panel from the top.
  resync: (_frame, context) => {
    coalescer.flush();
    (context.inbox ?? registered)?.gap?.();
  },
  // The reasons are the coordinator's and the gates' (ST-03).
  closed: noop,
};

export function handleFrame(frame: StreamFrameOut, context: FrameContext): void {
  const handler = (
    FRAME_HANDLERS as Record<string, ((f: StreamFrameOut, c: FrameContext) => void) | undefined>
  )[frame.type];
  handler?.(frame, context);
}

/** For the unit test: every event type of the vocabulary has an entry. */
export const HANDLED_EVENT_TYPES: readonly EventType[] = EVENT_TYPES;
