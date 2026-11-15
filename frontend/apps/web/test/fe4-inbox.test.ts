/**
 * FE-4's rules, without a browser (IL, TH, CP, RT, LY): each decision is a pure function or a small
 * class with an injected clock, timer, URL API or storage, so no test reads the wall clock, the
 * date, the machine's time zone or its load.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { EVENT_TYPES, queryKey } from "@wasla/api-client";
import { buildCsp } from "@wasla/session/headers";

import {
  clearDrafts,
  composerMode,
  counterVisible,
  draftKey,
  enterSends,
  readDraft,
  replyAssigns,
  writeDraft,
  type ConversationFacts,
  type StorageLike,
} from "../lib/inbox/composer";
import { conversationKey, inboxPrefix, messagesKey, unreadSummaryKey } from "../lib/inbox/keys";
import { BREAKPOINTS, layoutFor, panelStartsOpen, phoneStep } from "../lib/inbox/layout";
import {
  applyListUpdateNow,
  downSince,
  capCount,
  firstPageOnly,
  mayMarkRead,
  onlyAppends,
  pollInterval,
  restartsFromTop,
  shouldPoll,
  StreamWatch,
} from "../lib/inbox/live";
import {
  blobFits,
  checkFileToSend,
  countingUrls,
  Gate,
  MediaRegistry,
  safeFilename,
  SEND_MAX_BYTES,
  SEND_TYPES,
} from "../lib/inbox/media";
import { dedupeById, isRetryable, SendQueue } from "../lib/inbox/send-queue";
import { openConfirmed, safeHttpUrl, splitLinks } from "../lib/inbox/text";
import { durationParts, relativeTime, threadItems } from "../lib/inbox/time";
import {
  checkSearch,
  DEFAULT_VIEW,
  inboxHref,
  listQuery,
  parseView,
  viewParams,
  type InboxView,
} from "../lib/inbox/view";
import {
  Coalescer,
  FRAME_HANDLERS,
  handleFrame,
  HOME_CONVERSATIONS,
  refreshAfterGap,
  registerInboxHooks,
  setCoalescer,
} from "../lib/stream-handlers";

const ID = "0b9f3c4e-1a2b-4c3d-8e9f-001122334455";
const OTHER = "1c8e2d3f-2b3c-4d5e-9f0a-112233445566";
const scope = { workspaceId: "ws-1", sessionGeneration: 3 };

/** A coalescer whose timers run only when the test says so. */
function manualCoalescer() {
  const queue: (() => void)[] = [];
  const coalescer = new Coalescer(
    (run) => {
      queue.push(run);
      return queue.length as unknown as ReturnType<typeof setTimeout>;
    },
    () => undefined,
  );
  return { coalescer, flush: () => queue.splice(0).forEach((run) => run()) };
}

class MemoryStorage implements StorageLike {
  #map = new Map<string, string>();
  getItem(key: string) {
    return this.#map.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.#map.set(key, value);
  }
  removeItem(key: string) {
    this.#map.delete(key);
  }
  key(index: number) {
    return [...this.#map.keys()][index] ?? null;
  }
  get length() {
    return this.#map.size;
  }
}

describe("IL-02 / LY-03: the view in the URL, never the search term", () => {
  it("parses known values and drops anything else", () => {
    const view = parseView(
      new URLSearchParams(
        "status=closed&assigned_to=me&mode=human&priority=urgent&unread=1&q=nour",
      ),
    );
    expect(view).toEqual({
      status: "closed",
      assignedTo: "me",
      mode: "human",
      priority: "urgent",
      unread: true,
    });
    expect(parseView(new URLSearchParams("status=<x>&assigned_to=bob&mode=robot"))).toEqual(
      DEFAULT_VIEW,
    );
    expect(
      parseView(new URLSearchParams(`assigned_to=${ID.toLocaleUpperCase("en-US")}`)).assignedTo,
    ).toBe(ID);
  });
  it("builds addresses with the view and the step only (M-V12)", () => {
    const view: InboxView = { ...DEFAULT_VIEW, status: "pending", unread: true };
    expect(inboxHref(view)).toBe("/inbox?status=pending&unread=1");
    expect(inboxHref(view, ID)).toBe(`/inbox/${ID}?status=pending&unread=1`);
    expect(inboxHref(view, ID, { panel: true })).toBe(
      `/inbox/${ID}?status=pending&unread=1&panel=customer`,
    );
    expect(inboxHref(DEFAULT_VIEW, "not-an-id")).toBe("/inbox");
    expect(viewParams(view).has("q")).toBe(false);
    // The builder takes no search term at all.
    expect(inboxHref.length).toBeLessThanOrEqual(3);
  });
  it("the request carries q in memory, never updated_since with a view (M-V13)", () => {
    const query = listQuery({ ...DEFAULT_VIEW, mode: "ai" }, "nour", "abc", 30);
    expect(query).toEqual({ mode: "ai", q: "nour", cursor: "abc", limit: 30 });
    expect(Object.keys(listQuery(DEFAULT_VIEW, null, null, 30))).toEqual(["limit"]);
    expect(JSON.stringify(query)).not.toContain("updated_since");
  });
  it("the search bounds follow the API (2..100 after trimming)", () => {
    expect(checkSearch(" ")).toEqual({ kind: "none" });
    expect(checkSearch(" a ")).toEqual({ kind: "too-short" });
    expect(checkSearch("x".repeat(101))).toEqual({ kind: "too-long" });
    expect(checkSearch(" ab ")).toEqual({ kind: "ok", term: "ab" });
  });
});

describe("IL-04: a cursor of another view reads the list again from the top, once", () => {
  it("restarts only for cursor_view_mismatch on a next page, and only once in a row", () => {
    expect(
      restartsFromTop({ code: "cursor_view_mismatch", cursor: "c1", mismatchesInARow: 0 }),
    ).toBe(true);
    expect(
      restartsFromTop({ code: "cursor_view_mismatch", cursor: "c1", mismatchesInARow: 1 }),
    ).toBe(false);
    expect(
      restartsFromTop({ code: "cursor_view_mismatch", cursor: null, mismatchesInARow: 0 }),
    ).toBe(false);
    expect(restartsFromTop({ code: "validation_error", cursor: "c1", mismatchesInARow: 0 })).toBe(
      false,
    );
  });
  it("cuts the loaded pages to the first one, read from no cursor", () => {
    expect(
      firstPageOnly({ pages: ["one", "two", "empty"], pageParams: [null, "c1", "bad"] }),
    ).toEqual({ pages: ["one"], pageParams: [null] });
    expect(firstPageOnly(undefined)).toBeUndefined();
  });
});

describe("RT-04: since when the stream is down", () => {
  it("is set once and kept through reconnect attempts; an open stream clears it", () => {
    let since = downSince(null, "waiting", 1_000);
    expect(since).toBe(1_000);
    since = downSince(since, "connecting", 4_000);
    since = downSince(since, "waiting", 9_000);
    expect(since).toBe(1_000);
    expect(shouldPoll({ stream: "waiting", since: since ?? 0, now: 12_000, visible: true })).toBe(
      true,
    );
    expect(downSince(since, "open", 13_000)).toBeNull();
    expect(downSince(null, "stopped", 13_000)).toBeNull();
  });
});

describe("IL-06 / IL-07: the live list and the counts", () => {
  it("applies an update at once only at the top with no row focused", () => {
    expect(applyListUpdateNow({ scrolledAway: false, rowFocused: false })).toBe(true);
    expect(applyListUpdateNow({ scrolledAway: true, rowFocused: false })).toBe(false);
    expect(applyListUpdateNow({ scrolledAway: false, rowFocused: true })).toBe(false);
  });
  it("applies a loaded page at once, even scrolled away: only a reorder waits", () => {
    expect(onlyAppends(["a", "b"], ["a", "b", "c"])).toBe(true);
    expect(onlyAppends(["a", "b"], ["b", "a", "c"])).toBe(false);
    expect(onlyAppends(["a", "b"], ["a", "b"])).toBe(false);
    expect(onlyAppends(["a", "b", "c"], ["a", "b"])).toBe(false);
  });
  it("caps counts as 99+ and 999+", () => {
    expect(capCount(99, 99)).toBe("99");
    expect(capCount(100, 99)).toBe("99+");
    expect(capCount(1000, 999)).toBe("999+");
  });
  it("marks read only when visible, at the newest, with something unread (M-V31, M-V32)", () => {
    const base = {
      visible: true,
      newestInView: true,
      newestMessageId: "m2",
      readMessageId: "m1",
      unreadCount: 1,
      alreadySeen: null,
    };
    expect(mayMarkRead(base)).toBe(true);
    expect(mayMarkRead({ ...base, visible: false })).toBe(false);
    expect(mayMarkRead({ ...base, newestInView: false })).toBe(false);
    expect(mayMarkRead({ ...base, unreadCount: 0 })).toBe(false);
    expect(mayMarkRead({ ...base, alreadySeen: "m2" })).toBe(false);
    expect(mayMarkRead({ ...base, readMessageId: "m2" })).toBe(false);
  });
  it("FEF-35: a mark-as-unread is not undone by the screen that had the message in view", () => {
    // Seen m2 with nothing unread; someone marks it unread: the count rises, m2 is still newest.
    const after = {
      visible: true,
      newestInView: true,
      newestMessageId: "m2",
      readMessageId: "m1",
      unreadCount: 1,
      alreadySeen: "m2",
    };
    expect(mayMarkRead(after)).toBe(false);
    // A newer message in view is read as usual.
    expect(mayMarkRead({ ...after, newestMessageId: "m3" })).toBe(true);
  });
  it("a reply assigns only an unassigned conversation; opening never does (M-V38)", () => {
    expect(replyAssigns({ assigned_to_id: null })).toBe(true);
    expect(replyAssigns({ assigned_to_id: OTHER })).toBe(false);
    const source = readFileSync(
      join(__dirname, "../components/inbox/conversation-pane.tsx"),
      "utf8",
    );
    // The pane's only assignment call is the person's own menu choice.
    expect(source.match(/kind: "assign"/g)?.length ?? 0).toBeGreaterThan(0);
    expect(source).not.toMatch(/useEffect\([^)]*assign/s);
  });
  it("nothing about read state is kept in the browser (M-V34)", () => {
    for (const file of ["thread.tsx", "conversation-list-pane.tsx", "inbox-runtime.tsx"]) {
      const source = readFileSync(join(__dirname, "../components/inbox", file), "utf8");
      expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB|BroadcastChannel/);
    }
  });
  it("mark as unread leaves the conversation (M-V37)", () => {
    const source = readFileSync(
      join(__dirname, "../components/inbox/conversation-pane.tsx"),
      "utf8",
    );
    expect(source).toMatch(
      /onSuccess: \(\) => \{\s*refreshInboxLists\(queryClient, scope\);\s*onClosedUnread\(\);/,
    );
  });
});

describe("TH-02: customer text and links", () => {
  it("splits http(s) links out of text; nothing else is a link (M-V28)", () => {
    expect(splitLinks("see https://example.test/a?b=1. thanks")).toEqual([
      { kind: "text", text: "see " },
      { kind: "link", text: "https://example.test/a?b=1", url: "https://example.test/a?b=1" },
      { kind: "text", text: ". thanks" },
    ]);
    expect(splitLinks("javascript:alert(1) and ftp://x.test")).toEqual([
      { kind: "text", text: "javascript:alert(1) and ftp://x.test" },
    ]);
    expect(safeHttpUrl("https://user:pw@example.test")).toBeNull();
  });
  it("opens only after the confirmation, without opener or referrer", () => {
    const open = vi.fn();
    openConfirmed("https://example.test/x", open);
    expect(open).toHaveBeenCalledWith("https://example.test/x", "_blank", "noopener,noreferrer");
    openConfirmed("javascript:alert(1)", open);
    expect(open).toHaveBeenCalledTimes(1);
  });
  it("customer text is never markup in the inbox (M-V27, M-V33)", () => {
    for (const file of [
      "thread.tsx",
      "conversation-list-pane.tsx",
      "media.tsx",
      "customer-panel.tsx",
    ]) {
      const source = readFileSync(join(__dirname, "../components/inbox", file), "utf8");
      expect(source).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/);
      expect(source).not.toMatch(/<a\s[^>]*href=\{part/);
    }
  });
  it("groups same-side messages within five minutes, a separator per day", () => {
    const at = (minutes: number) => new Date(Date.UTC(2026, 10, 14, 23, minutes)).toISOString();
    const items = threadItems(
      [
        { id: "a", side: "in" as const, createdAt: at(0) },
        { id: "b", side: "in" as const, createdAt: at(4) },
        { id: "c", side: "out" as const, createdAt: at(5) },
        { id: "d", side: "out" as const, createdAt: at(70) },
      ],
      "UTC",
    );
    expect(items.map((item) => (item.kind === "day" ? `day:${item.day}` : item.key))).toEqual([
      "day:2026-11-14",
      "a",
      "b",
      "c",
      "day:2026-11-15",
      "d",
    ]);
    const b = items.find((item) => item.key === "b");
    expect(b?.kind === "message" && b.firstOfGroup).toBe(false);
  });
  it("relative times use an injected clock and Latin digits", () => {
    const now = Date.UTC(2026, 10, 14, 12, 0);
    expect(relativeTime(new Date(now - 5 * 60_000).toISOString(), now, "en")).toBe("5 minutes ago");
    expect(relativeTime(new Date(now - 5 * 60_000).toISOString(), now, "ar")).toMatch(/5/);
    expect(durationParts(80 * 60_000)).toEqual({ hours: 1, minutes: 20 });
  });
});

describe("TH-04 / TH-05 / CP-03: media", () => {
  it("revokes a URL when its last owner lets go, and all on revokeAll (M-V15)", () => {
    const urls = countingUrls({
      createObjectURL: () => `blob:${Math.random()}`,
      revokeObjectURL: () => undefined,
    });
    const registry = new MediaRegistry(urls, { clock: () => 1 });
    const blob = new Blob(["x"], { type: "image/png" });
    const first = registry.acquire("m1", "tile-a", blob);
    expect(registry.acquire("m1", "tile-b")).toBe(first);
    registry.release("m1", "tile-a");
    expect(urls.revoked).toBe(0);
    registry.release("m1", "tile-b");
    expect(urls.revoked).toBe(1);
    registry.acquire("m2", "t", blob);
    registry.acquire("m3", "t", blob);
    registry.revokeAll();
    expect(urls.created).toBe(urls.revoked);
  });
  it("keeps at most the byte bound, oldest unheld first", () => {
    const urls = countingUrls({
      createObjectURL: () => "blob:x",
      revokeObjectURL: () => undefined,
    });
    let clock = 0;
    const registry = new MediaRegistry(urls, { clock: () => (clock += 1), limitBytes: 10 });
    registry.acquire("old", "a", new Blob(["123456"]));
    registry.release("old", "a");
    registry.acquire("old", "a", new Blob(["123456"]));
    registry.acquire("new", "b", new Blob(["123456"]));
    expect(registry.bytes).toBeLessThanOrEqual(10);
    expect(registry.has("new")).toBe(true);
  });
  it("draws a blob only in a tile of its own kind (M-V16)", () => {
    expect(blobFits("image", "image/png")).toBe(true);
    expect(blobFits("image", "text/html")).toBe(false);
    expect(blobFits("image", "image/svg+xml")).toBe(false);
    expect(blobFits("audio", "audio/ogg; codecs=opus")).toBe(true);
    expect(blobFits("video", "audio/ogg")).toBe(false);
  });
  it("never puts an API media URL in the DOM (M-V14)", () => {
    const tile = readFileSync(join(__dirname, "../components/inbox/media.tsx"), "utf8");
    expect(tile).not.toMatch(/src=\{[^}]*\/media\//);
    expect(tile).toMatch(/parseAs: "blob"/);
  });
  it("at most three fetches at a time", async () => {
    const gate = new Gate(3);
    let peak = 0;
    let running = 0;
    const job = () =>
      gate.run(async () => {
        running += 1;
        peak = Math.max(peak, running);
        await Promise.resolve();
        running -= 1;
      });
    await Promise.all(Array.from({ length: 8 }, job));
    expect(peak).toBe(3);
  });
  it("checks a file before any request", () => {
    expect(checkFileToSend({ type: "image/png", size: 10 })).toEqual({ ok: true });
    expect(checkFileToSend({ type: "image/png", size: SEND_MAX_BYTES + 1 })).toEqual({
      ok: false,
      reason: "size",
    });
    expect(checkFileToSend({ type: "application/x-msdownload", size: 10 })).toEqual({
      ok: false,
      reason: "type",
    });
    expect(checkFileToSend({ type: "text/plain", size: 0 })).toEqual({
      ok: false,
      reason: "empty",
    });
  });
  it("the send types are the backend's canonical list", () => {
    const backend = readFileSync(join(__dirname, "../../../../app/core/media_types.py"), "utf8");
    for (const type of SEND_TYPES) expect(backend).toContain(`"${type}"`);
  });
  it("a download name has no path and no control character", () => {
    expect(safeFilename("../../etc/pa\u0007ss<wd>.pdf", "Document")).toBe(
      "pass wd.pdf".replace(" ", ""),
    );
    expect(safeFilename(null, "Document")).toBe("Document");
  });
  it("the CSP allows blob: for images and media only (M-V17)", () => {
    const csp = buildCsp({
      nonce: "abcdefghijklmnop",
      apiOrigin: "https://api.test",
      devServer: false,
    });
    const directives: Record<string, string[]> = {};
    for (const part of csp.split(";")) {
      const [name = "", ...values] = part.trim().split(/\s+/);
      directives[name] = values;
    }
    expect(directives["img-src"]).toContain("blob:");
    expect(directives["media-src"]).toContain("blob:");
    for (const name of ["default-src", "script-src", "connect-src", "frame-src", "object-src"]) {
      expect(directives[name] ?? []).not.toContain("blob:");
    }
  });
});

describe("CP-01 .. CP-04: the composer", () => {
  const key = (init: Partial<Parameters<typeof enterSends>[0]>) => ({
    key: "Enter",
    shiftKey: false,
    ...init,
  });
  it("Enter sends on a keyboard device, Shift+Enter is a new line, never during IME (M-V20)", () => {
    expect(enterSends(key({}), false)).toBe(true);
    expect(enterSends(key({ shiftKey: true }), false)).toBe(false);
    expect(enterSends(key({ isComposing: true }), false)).toBe(false);
    expect(enterSends(key({ keyCode: 229 }), false)).toBe(false);
    expect(enterSends(key({ repeat: true }), false)).toBe(false);
    expect(enterSends(key({}), true)).toBe(false);
    expect(enterSends({ key: "a", shiftKey: false }, false)).toBe(false);
  });
  it("the draft is per workspace and conversation, and goes on sign-out (M-V21)", () => {
    const storage = new MemoryStorage();
    writeDraft(storage, "ws-1", ID, "hello");
    writeDraft(storage, "ws-2", ID, "other");
    expect(readDraft(storage, "ws-1", ID)).toBe("hello");
    expect(draftKey("ws-1", ID)).toBe(`wasla-draft:ws-1:${ID}`);
    writeDraft(storage, "ws-1", ID, "");
    expect(readDraft(storage, "ws-1", ID)).toBe("");
    writeDraft(storage, "ws-1", ID, "again");
    clearDrafts(storage);
    expect(storage.length).toBe(0);
    const draftFile = readFileSync(join(__dirname, "../lib/inbox/draft-storage.ts"), "utf8");
    expect(draftFile).toMatch(/window\.sessionStorage/);
    expect(draftFile).not.toMatch(/window\.localStorage|[^.\w]localStorage\./);
  });
  const facts = (
    overrides: Partial<ConversationFacts["reply_policy"]> & { status?: string } = {},
  ): ConversationFacts => ({
    status: overrides.status ?? "open",
    mode: "ai",
    assigned_to_id: null,
    reply_policy: {
      free_text_allowed: true,
      window_expires_at: null,
      out_of_window: "template",
      templates: true,
      text_limit: 4096,
      state: "operational",
      ...overrides,
    },
  });
  it("the reply window decides between free text and templates (M-V22)", () => {
    expect(composerMode(facts({ free_text_allowed: false }), { now: 0, optedOut: false })).toEqual({
      kind: "templates-only",
    });
    expect(composerMode(facts(), { now: 0, optedOut: false })).toEqual({
      kind: "free",
      expiresInMs: null,
    });
    const ends = new Date(Date.UTC(2026, 10, 14, 13, 0)).toISOString();
    expect(
      composerMode(facts({ window_expires_at: ends }), {
        now: Date.UTC(2026, 10, 14, 12, 0),
        optedOut: false,
      }),
    ).toEqual({ kind: "free", expiresInMs: 3_600_000 });
    expect(composerMode(facts({ status: "closed" }), { now: 0, optedOut: false })).toEqual({
      kind: "closed",
    });
    expect(composerMode(facts(), { now: 0, optedOut: true })).toEqual({ kind: "opted-out" });
    expect(composerMode(facts({ state: "paused" }), { now: 0, optedOut: false })).toEqual({
      kind: "unavailable",
    });
  });
  it("the counter shows in the last 10 %", () => {
    expect(counterVisible(3685, 4096)).toBe(false);
    expect(counterVisible(3686, 4096)).toBe(true);
  });
});

describe("CP-02: one Idempotency-Key per bubble", () => {
  let n = 0;
  const deps = { newId: () => `id-${(n += 1)}`, now: () => n };
  it("ten failed answers and a 201: one bubble, one key (M-V18)", () => {
    const queue = new SendQueue(deps);
    const send = queue.add({ conversationId: ID, kind: "text", text: "hi" });
    const keys = new Set<string>();
    for (let attempt = 0; attempt < 10; attempt += 1) {
      queue.fail(send.clientId, { code: "network_error", retryable: true });
      const again = queue.retry(send.clientId)!;
      keys.add(again.idempotencyKey);
    }
    expect(keys).toEqual(new Set([send.idempotencyKey]));
    expect(queue.forConversation(ID)).toHaveLength(1);
    queue.settle(send.clientId, "m-1");
    expect(queue.forConversation(ID)).toHaveLength(0);
    expect(queue.settledIds()).toEqual(new Set(["m-1"]));
  });
  it("a frame or refetch carrying the same id never draws twice (M-V19)", () => {
    const merged = dedupeById([{ id: "m-1" }, { id: "m-2" }, { id: "m-1" }]);
    expect(merged.map((item) => item.id)).toEqual(["m-1", "m-2"]);
  });
  it("sends of one conversation go in order", async () => {
    const queue = new SendQueue(deps);
    const order: string[] = [];
    let release!: () => void;
    const first = queue.enqueue(
      ID,
      () =>
        new Promise<void>(
          (resolve) =>
            (release = () => {
              order.push("a");
              resolve();
            }),
        ),
    );
    const second = queue.enqueue(ID, () => {
      order.push("b");
      return Promise.resolve();
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(order).toEqual([]);
    release();
    await Promise.all([first, second]);
    expect(order).toEqual(["a", "b"]);
  });
  it("retries only what a retry can fix", () => {
    expect(isRetryable(null, "network_error")).toBe(true);
    expect(isRetryable(503, "dependency_unavailable")).toBe(true);
    expect(isRetryable(409, "retryable_conflict")).toBe(true);
    expect(isRetryable(422, "recipient_opted_out")).toBe(false);
    expect(isRetryable(422, "reply_window_closed")).toBe(false);
    expect(isRetryable(409, "conflict")).toBe(false);
  });
});

describe("RT-03: a gap reads everything a frame would have moved (FEF-37)", () => {
  it("the lists' first pages, the unread total, the open conversation, its newest page", () => {
    const queryClient = new QueryClient();
    const gapScope = { workspaceId: "w", sessionGeneration: 1 };
    const listKey = [...inboxPrefix(gapScope), "open"];
    queryClient.setQueryData(listKey, {
      pages: [{ items: [] }, { items: [] }],
      pageParams: [null, "c"],
    });
    queryClient.setQueryData(unreadSummaryKey(gapScope), { unread_messages: 0 });
    queryClient.setQueryData(conversationKey(gapScope, ID), { id: ID });
    queryClient.setQueryData(messagesKey(gapScope, ID), {
      pages: [{ items: [] }, { items: [] }],
      pageParams: [null, "m"],
    });
    queryClient.setQueryData(conversationKey(gapScope, "other"), { id: "other" });
    refreshAfterGap(queryClient, gapScope, ID);
    const invalidated = (key: readonly unknown[]) =>
      queryClient.getQueryState(key)?.isInvalidated ?? false;
    expect(invalidated(listKey)).toBe(true);
    expect(invalidated(unreadSummaryKey(gapScope))).toBe(true);
    expect(invalidated(conversationKey(gapScope, ID))).toBe(true);
    expect(invalidated(messagesKey(gapScope, ID))).toBe(true);
    expect(invalidated(conversationKey(gapScope, "other"))).toBe(false);
    expect((queryClient.getQueryData(listKey) as { pages: unknown[] }).pages).toHaveLength(1);
    expect(
      (queryClient.getQueryData(messagesKey(gapScope, ID)) as { pages: unknown[] }).pages,
    ).toHaveLength(1);
  });
});

describe("RT-01 .. RT-04: the stream's invalidation map", () => {
  it("has an entry for every event type of the vocabulary and the control frames", () => {
    expect(Object.keys(FRAME_HANDLERS).sort()).toEqual([...EVENT_TYPES, "closed", "resync"].sort());
  });
  it("message.created: home at once; the lists, the summary, the conversation and the after= page once per burst (M-V24)", () => {
    const { coalescer, flush } = manualCoalescer();
    setCoalescer(coalescer);
    const queryClient = new QueryClient();
    const catchUp = vi.fn();
    const listKey = [...inboxPrefix(scope), { status: null }] as const;
    queryClient.setQueryData(listKey, {
      pages: [{ items: [] }, { items: [] }],
      pageParams: [null, "c"],
    });
    queryClient.setQueryData(queryKey(scope, HOME_CONVERSATIONS), { items: [] });
    queryClient.setQueryData(messagesKey(scope, ID), {
      pages: [{ items: [] }],
      pageParams: [null],
    });
    queryClient.setQueryData(unreadSummaryKey(scope), { unread_messages: 0 });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    for (let index = 0; index < 5; index += 1) {
      handleFrame(
        { type: "message.created", id: `${index}-0`, data: { conversation_id: ID } },
        { queryClient, scope, inbox: { catchUp } },
      );
    }
    expect(queryClient.getQueryState(queryKey(scope, HOME_CONVERSATIONS))?.isInvalidated).toBe(
      true,
    );
    expect(catchUp).not.toHaveBeenCalled();
    flush();
    expect(catchUp).toHaveBeenCalledTimes(1);
    expect(catchUp).toHaveBeenCalledWith(ID);
    const lists = invalidate.mock.calls.filter(
      ([filters]) => JSON.stringify(filters?.queryKey) === JSON.stringify(inboxPrefix(scope)),
    );
    expect(lists).toHaveLength(1);
    expect((queryClient.getQueryData(listKey) as { pages: unknown[] }).pages).toHaveLength(1);
    expect(queryClient.getQueryState(unreadSummaryKey(scope))?.isInvalidated).toBe(true);
    setCoalescer(new Coalescer());
  });
  it("message.status patches the loaded message, no request (M-V25)", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(messagesKey(scope, ID), {
      pages: [{ items: [{ id: "m1", status: "sent" }], next_cursor: null }],
      pageParams: [null],
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    handleFrame(
      {
        type: "message.status",
        id: "1-0",
        data: { conversation_id: ID, message_id: "m1", status: "read" },
      },
      { queryClient, scope },
    );
    expect(invalidate).not.toHaveBeenCalled();
    const data = queryClient.getQueryData(messagesKey(scope, ID)) as {
      pages: { items: { status: string }[] }[];
    };
    expect(data.pages[0]!.items[0]!.status).toBe("read");
  });
  it("conversation.updated: the conversation, the lists, and the summary for read or status", () => {
    const { coalescer, flush } = manualCoalescer();
    setCoalescer(coalescer);
    const queryClient = new QueryClient();
    queryClient.setQueryData(conversationKey(scope, ID), { id: ID });
    queryClient.setQueryData(unreadSummaryKey(scope), { unread_messages: 1 });
    handleFrame(
      {
        type: "conversation.updated",
        id: "1-0",
        data: { conversation_id: ID, fields: ["priority"] },
      },
      { queryClient, scope },
    );
    flush();
    expect(queryClient.getQueryState(conversationKey(scope, ID))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(unreadSummaryKey(scope))?.isInvalidated).toBe(false);
    handleFrame(
      { type: "conversation.updated", id: "2-0", data: { conversation_id: ID, fields: ["read"] } },
      { queryClient, scope },
    );
    flush();
    expect(queryClient.getQueryState(unreadSummaryKey(scope))?.isInvalidated).toBe(true);
    setCoalescer(new Coalescer());
  });
  it("resync runs the inbox's gap rule; the registered hooks are used by default", () => {
    const gap = vi.fn();
    const unregister = registerInboxHooks({ gap });
    handleFrame({ type: "resync", id: null, data: {} }, { queryClient: new QueryClient(), scope });
    expect(gap).toHaveBeenCalledTimes(1);
    unregister();
    handleFrame({ type: "resync", id: null, data: {} }, { queryClient: new QueryClient(), scope });
    expect(gap).toHaveBeenCalledTimes(1);
  });
  it("six tabs: each applies a frame once and reads only what it holds", () => {
    const tabs = Array.from({ length: 6 }, (_, index) => {
      const queryClient = new QueryClient();
      if (index === 0)
        queryClient.setQueryData(messagesKey(scope, ID), {
          pages: [{ items: [] }],
          pageParams: [null],
        });
      return { queryClient, catchUp: vi.fn() };
    });
    const { coalescer, flush } = manualCoalescer();
    setCoalescer(coalescer);
    for (const tab of tabs) {
      handleFrame(
        { type: "message.created", id: "9-0", data: { conversation_id: ID } },
        {
          queryClient: tab.queryClient,
          scope: { ...scope, workspaceId: `ws-${tabs.indexOf(tab)}` },
          inbox: { catchUp: tab.catchUp },
        },
      );
    }
    flush();
    expect(tabs[0]!.catchUp).toHaveBeenCalledTimes(0);
    setCoalescer(new Coalescer());
  });
  it("polls only while the stream is down past its quiet period, never hidden (M-V26)", () => {
    expect(shouldPoll({ stream: "open", since: 0, now: 60_000, visible: true })).toBe(false);
    expect(shouldPoll({ stream: "waiting", since: 0, now: 5_000, visible: true })).toBe(false);
    expect(shouldPoll({ stream: "waiting", since: 0, now: 10_000, visible: true })).toBe(true);
    expect(shouldPoll({ stream: "ended", since: 0, now: 60_000, visible: false })).toBe(false);
    expect(pollInterval(60)).toBe(60_000);
    expect(pollInterval(null)).toBe(30_000);
  });
  it("a reopened stream is a gap; so is the first open seen after it was closed (FEF-37)", () => {
    const watch = new StreamWatch();
    expect(watch.next("connecting")).toBe(false);
    expect(watch.next("open")).toBe(true);
    expect(watch.next("open")).toBe(false);
    expect(watch.next("waiting")).toBe(false);
    expect(watch.next("open")).toBe(true);
    expect(watch.next("open")).toBe(false);
    // Already open when the inbox mounts: its frames cover everything after the first read.
    const mounted = new StreamWatch();
    expect(mounted.next("open")).toBe(false);
  });
});

describe("LY-01: three widths", () => {
  it("panes from 1024, a drawer from 768, steps below", () => {
    expect(layoutFor(390)).toBe("steps");
    expect(layoutFor(800)).toBe("drawer");
    expect(layoutFor(1024)).toBe("panes");
    expect(panelStartsOpen(1280)).toBe(true);
    expect(panelStartsOpen(1100)).toBe(false);
    expect(phoneStep(null, false)).toBe("list");
    expect(phoneStep(ID, false)).toBe("conversation");
    expect(phoneStep(ID, true)).toBe("panel");
  });
  it("the breakpoints are the tokens'", () => {
    const tokens = readFileSync(join(__dirname, "../../../packages/ui/src/tokens.css"), "utf8");
    expect(tokens).toContain(`--breakpoint-md: ${BREAKPOINTS.md}px`);
    expect(tokens).toContain(`--breakpoint-lg: ${BREAKPOINTS.lg}px`);
    expect(tokens).toContain(`--breakpoint-xl: ${BREAKPOINTS.xl}px`);
  });
});
