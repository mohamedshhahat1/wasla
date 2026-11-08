/* eslint-disable @typescript-eslint/require-await -- test doubles implement async interfaces without awaiting */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { CLOSE_REASONS } from "../src/events";
import {
  openEventStream,
  RECONNECT_CLOSE_REASONS,
  streamBackoff,
  TERMINAL_CLOSE_REASONS,
  type EventSourceLike,
  type StreamState,
  type StreamTimer,
} from "../src/stream";

const API = "https://api.example";

class FakeSource implements EventSourceLike {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  constructor(readonly url: string) {}
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  emit(type: string, data: unknown, id = "") {
    const event = new MessageEvent(type, { data: JSON.stringify(data), lastEventId: id });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  /** EventSource's own reconnect with the same (spent) URL: CONNECTING, then error. */
  ownReconnect() {
    this.readyState = 0;
    this.onerror?.(new Event("error"));
  }
  /** A non-200 answer: CLOSED, then error. */
  refused() {
    this.readyState = 2;
    this.onerror?.(new Event("error"));
  }
}

/** A manual timer: nothing waits; the test fires due callbacks. */
function manualTimer() {
  const queue: Array<{ callback: () => void; ms: number; handle: number }> = [];
  let next = 1;
  const timer: StreamTimer = {
    setTimeout: (callback, ms) => {
      const handle = next++;
      queue.push({ callback, ms, handle });
      return handle;
    },
    clearTimeout: (handle) => {
      const index = queue.findIndex((item) => item.handle === handle);
      if (index >= 0) queue.splice(index, 1);
    },
  };
  return {
    timer,
    queue,
    fire() {
      const item = queue.shift();
      item?.callback();
      return item?.ms;
    },
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function harness(ticketStatuses: number[] = []) {
  const sources: FakeSource[] = [];
  const states: StreamState[] = [];
  const frames: Array<{ type: string; data: unknown }> = [];
  const time = manualTimer();
  let tickets = 0;
  let refreshes = 0;
  const fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
    const status = ticketStatuses.shift() ?? 201;
    expect(new Headers(init?.headers).get("Authorization")).toMatch(/^Bearer /);
    if (status !== 201) return new Response("{}", { status });
    tickets += 1;
    return new Response(JSON.stringify({ ticket: `t${tickets}`, expires_at: "x" }), {
      status: 201,
    });
  };
  const stream = openEventStream({
    api: API,
    getAccessToken: async () => "access",
    onUnauthenticated: async () => {
      refreshes += 1;
      return "access-2";
    },
    onFrame: (frame) => frames.push(frame),
    onState: (state) => states.push(state),
    fetch,
    eventSource: (url) => {
      const source = new FakeSource(url);
      sources.push(source);
      return source;
    },
    timer: time.timer,
    random: () => 1,
  });
  return {
    sources,
    states,
    frames,
    time,
    stream,
    get tickets() {
      return tickets;
    },
    get refreshes() {
      return refreshes;
    },
  };
}

describe("openEventStream (FX-07)", () => {
  it("its close-reason table is event-types.json's", () => {
    const file = JSON.parse(
      readFileSync(new URL("../event-types.json", import.meta.url), "utf8"),
    ) as { close_reasons?: string[]; closeReasons?: string[] };
    const fromFile = (file.close_reasons ?? file.closeReasons ?? []).slice().sort();
    expect([...TERMINAL_CLOSE_REASONS, ...RECONNECT_CLOSE_REASONS].sort()).toEqual(fromFile);
    expect([...CLOSE_REASONS].sort()).toEqual(fromFile);
  });

  it("backs off 1, 2, 4 … 30 s, jittered between 50 % and 100 %", () => {
    expect([1, 2, 3, 6, 9].map((n) => streamBackoff(n, () => 1))).toEqual([
      1000, 2000, 4000, 30000, 30000,
    ]);
    expect(streamBackoff(3, () => 0)).toBe(2000);
  });

  it("takes a ticket and opens the stream on the API with it; frames arrive", async () => {
    const h = harness();
    await flush();
    expect(h.sources).toHaveLength(1);
    expect(h.sources[0]!.url).toBe(`${API}/api/v1/events/stream?ticket=t1`);
    h.sources[0]!.open();
    h.sources[0]!.emit("message.created", { message_id: "m1" }, "e1");
    expect(h.frames).toEqual([{ type: "message.created", id: "e1", data: { message_id: "m1" } }]);
    expect(h.states.at(-1)).toEqual({ kind: "open", ticketNumber: 1 });
  });

  it("server_restart: closes it and opens a NEW EventSource with a NEW ticket (PV-03)", async () => {
    const h = harness();
    await flush();
    h.sources[0]!.open();
    h.sources[0]!.emit("closed", { reason: "server_restart" });
    expect(h.sources[0]!.closed).toBe(true);
    expect(h.time.fire()).toBe(1000);
    await flush();
    expect(h.sources).toHaveLength(2);
    expect(h.sources[1]!.url).toContain("ticket=t2");
    h.sources[1]!.open();
    h.sources[1]!.emit("message.created", { message_id: "m2" });
    expect(h.frames.at(-1)).toMatchObject({ type: "message.created" });
  });

  it("EventSource's own reconnect with the spent ticket is never relied on", async () => {
    const h = harness();
    await flush();
    h.sources[0]!.open();
    h.sources[0]!.ownReconnect();
    expect(h.sources[0]!.closed).toBe(true);
    h.time.fire();
    await flush();
    expect(h.sources[1]!.url).toContain("ticket=t2");
  });

  it("a refused EventSource (CLOSED) takes a new ticket; repeated failures back off further", async () => {
    const h = harness();
    await flush();
    h.sources[0]!.refused();
    expect(h.time.fire()).toBe(1000);
    await flush();
    h.sources[1]!.refused();
    expect(h.time.fire()).toBe(2000);
    await flush();
    h.sources[2]!.open();
    h.sources[2]!.refused();
    expect(h.time.fire()).toBe(1000);
  });

  it.each(["membership_revoked", "workspace_unavailable", "workspace_switched"])(
    "%s ends the stream for good",
    async (reason) => {
      const h = harness();
      await flush();
      h.sources[0]!.open();
      h.sources[0]!.emit("closed", { reason });
      expect(h.states.at(-1)).toEqual({ kind: "closed", reason });
      expect(h.time.queue).toHaveLength(0);
      expect(h.sources).toHaveLength(1);
    },
  );

  it("a 401 on the ticket refreshes once and tries again; a second refusal ends it", async () => {
    const h = harness([401]);
    await flush();
    expect(h.refreshes).toBe(1);
    expect(h.sources).toHaveLength(1);
    const g = harness([401, 401]);
    await flush();
    expect(g.states.at(-1)).toEqual({ kind: "closed", reason: "unauthenticated" });
  });

  it("a 5xx on the ticket backs off and retries", async () => {
    const h = harness([503]);
    await flush();
    expect(h.sources).toHaveLength(0);
    expect(h.time.fire()).toBe(1000);
    await flush();
    expect(h.sources).toHaveLength(1);
  });

  it("close() stops everything, including a pending reconnect", async () => {
    const h = harness();
    await flush();
    h.sources[0]!.refused();
    expect(h.time.queue).toHaveLength(1);
    h.stream.close();
    expect(h.time.queue).toHaveLength(0);
    expect(h.states.at(-1)).toEqual({ kind: "stopped" });
  });
});
