/**
 * ST-01..ST-06: one stream per browser per workspace, with fake Web Locks, a fake
 * BroadcastChannel and a fake stream opener - no browser, no timer, no storage.
 */
import { describe, expect, it } from "vitest";

import type { StreamFrameOut, StreamState } from "@wasla/api-client/stream";

import {
  StreamCoordinator,
  streamLockName,
  type OpenStreamHandlers,
  type StreamChannel,
  type StreamLocks,
} from "../lib/stream-coordinator";
import { switchTo } from "../lib/switch";

/** Web Locks with an exclusive queue per name and AbortSignal support. */
class FakeLocks implements StreamLocks {
  queues = new Map<string, Array<{ grant: () => void; signal: AbortSignal | undefined }>>();
  held = new Map<string, boolean>();

  request(
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<void>,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const grant = () => {
        this.held.set(name, true);
        void callback().then(() => {
          this.held.set(name, false);
          resolve();
          this.next(name);
        });
      };
      const queue = this.queues.get(name) ?? [];
      this.queues.set(name, queue);
      const entry = { grant, signal: options.signal };
      options.signal?.addEventListener("abort", () => {
        const index = queue.indexOf(entry);
        if (index >= 0) {
          queue.splice(index, 1);
          reject(new DOMException("aborted", "AbortError"));
        }
      });
      if (!this.held.get(name)) grant();
      else queue.push(entry);
    });
  }

  next(name: string): void {
    const queue = this.queues.get(name) ?? [];
    const entry = queue.shift();
    entry?.grant();
  }
}

/** BroadcastChannel: every other open channel of the same name receives a structured clone. */
class Hub {
  channels = new Set<FakeChannel>();
  posted: unknown[] = [];
  open(name: string): StreamChannel {
    const channel = new FakeChannel(this, name);
    this.channels.add(channel);
    return channel;
  }
}

class FakeChannel implements StreamChannel {
  listeners = new Set<(event: MessageEvent) => void>();
  closed = false;
  constructor(
    private hub: Hub,
    private name: string,
  ) {}
  postMessage(message: unknown): void {
    if (this.closed) throw new Error("posted on a closed channel");
    this.hub.posted.push(message);
    for (const other of this.hub.channels) {
      if (other === this || other.closed || other.name !== this.name) continue;
      const data = structuredClone(message);
      for (const listener of other.listeners) listener({ data } as MessageEvent);
    }
  }
  addEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.listeners.add(listener);
  }
  removeEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.listeners.delete(listener);
  }
  close(): void {
    this.closed = true;
    this.hub.channels.delete(this);
  }
}

interface Opened {
  handlers: OpenStreamHandlers;
  lastEventId: string | null;
  closed: boolean;
}

function world() {
  const locks = new FakeLocks();
  const hub = new Hub();
  const opened: Opened[] = [];
  const make = (workspace = "ws-1") =>
    new StreamCoordinator(workspace, {
      locks,
      channel: (name) => hub.open(name),
      open: (handlers, lastEventId) => {
        const stream: Opened = { handlers, lastEventId, closed: false };
        opened.push(stream);
        return {
          close() {
            stream.closed = true;
          },
        };
      },
    });
  return { locks, hub, opened, make };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const frame = (id: string, type = "message.created"): StreamFrameOut => ({
  type,
  id,
  data: { n: id },
});

describe("one stream per browser per workspace", () => {
  it("six tabs, one lock name, one stream; the first is the leader (M-Z14)", async () => {
    const { opened, make } = world();
    const tabs = Array.from({ length: 6 }, () => make());
    for (const tab of tabs) tab.start();
    await flush();
    expect(opened).toHaveLength(1);
    expect(tabs.map((tab) => tab.role)).toEqual([
      "leader",
      "waiting-for-lock",
      "waiting-for-lock",
      "waiting-for-lock",
      "waiting-for-lock",
      "waiting-for-lock",
    ]);
    expect(tabs[0]!.name).toBe(streamLockName("ws-1"));
  });

  it("every tab sees every frame exactly once, and the shared state", async () => {
    const { opened, make } = world();
    const tabs = Array.from({ length: 3 }, () => make());
    const seen = tabs.map(() => [] as string[]);
    tabs.forEach((tab, index) => tab.onFrame((item) => seen[index]!.push(item.id!)));
    for (const tab of tabs) tab.start();
    await flush();
    const stream = opened[0]!;
    stream.handlers.onState({ kind: "open", ticketNumber: 1 } satisfies StreamState);
    stream.handlers.onFrame(frame("1-0"));
    stream.handlers.onFrame(frame("2-0"));
    expect(seen).toEqual([
      ["1-0", "2-0"],
      ["1-0", "2-0"],
      ["1-0", "2-0"],
    ]);
    expect(tabs.map((tab) => tab.state.kind)).toEqual(["open", "open", "open"]);
  });

  it("the leader leaves: the next tab leads with a new stream from the last id it saw", async () => {
    const { opened, make } = world();
    const [first, second, third] = [make(), make(), make()];
    first.start();
    second.start();
    third.start();
    await flush();
    opened[0]!.handlers.onFrame(frame("7-0"));
    first.stop();
    await flush();
    expect(opened[0]!.closed).toBe(true);
    expect(opened).toHaveLength(2);
    expect(second.role).toBe("leader");
    expect(third.role).toBe("waiting-for-lock");
    expect(opened[1]!.lastEventId).toBe("7-0");
  });

  it("a tab that leaves before it leads never opens a stream", async () => {
    const { opened, make } = world();
    const [first, second] = [make(), make()];
    first.start();
    second.start();
    second.stop();
    first.stop();
    await flush();
    expect(opened).toHaveLength(1);
    expect(second.role).toBe("stopped");
  });

  it("a newcomer asks the leader for the state it has missed", async () => {
    const { opened, make } = world();
    const leader = make();
    leader.start();
    await flush();
    opened[0]!.handlers.onState({ kind: "open", ticketNumber: 1 });
    const late = make();
    late.start();
    await flush();
    expect(late.state.kind).toBe("open");
  });

  it("'Reconnect here' in a follower asks the leader to open once more", async () => {
    const { opened, make } = world();
    const [leader, follower] = [make(), make()];
    leader.start();
    follower.start();
    await flush();
    opened[0]!.handlers.onState({ kind: "closed", reason: "workspace_switched" });
    expect(follower.state).toEqual({ kind: "ended", reason: "workspace_switched" });
    follower.reconnect();
    expect(opened).toHaveLength(2);
    expect(opened[0]!.closed).toBe(true);
  });

  it("another workspace has its own lock and its own stream", async () => {
    const { opened, make } = world();
    make("ws-1").start();
    make("ws-2").start();
    await flush();
    expect(opened).toHaveLength(2);
  });

  it("frames and states posted carry no token or ticket", async () => {
    const { hub, opened, make } = world();
    make().start();
    make().start();
    await flush();
    opened[0]!.handlers.onFrame(frame("3-0"));
    opened[0]!.handlers.onState({ kind: "waiting", attempt: 1, delayMs: 1000 });
    const text = JSON.stringify(hub.posted);
    expect(text).not.toMatch(/ticket|access|token|bearer/i);
  });
});

describe("ST-05: a switch leaves before it adopts the new token (M-Z15)", () => {
  it("leave, then switch; rejoin only when the switch fails", async () => {
    const order: string[] = [];
    await switchTo("beta", {
      leave: () => order.push("leave"),
      switchWorkspace: (slug) => {
        order.push(`switch:${slug}`);
        return Promise.resolve();
      },
      rejoin: () => order.push("rejoin"),
    });
    expect(order).toEqual(["leave", "switch:beta"]);

    const failed: string[] = [];
    await expect(
      switchTo("gamma", {
        leave: () => failed.push("leave"),
        switchWorkspace: () => {
          failed.push("switch");
          return Promise.reject(new Error("refused"));
        },
        rejoin: () => failed.push("rejoin"),
      }),
    ).rejects.toThrow("refused");
    expect(failed).toEqual(["leave", "switch", "rejoin"]);
  });

  it("the old workspace's stream is closed by leaving, before anything else", async () => {
    const { opened, make } = world();
    const tab = make();
    tab.start();
    await flush();
    let closedWhenSwitching = false;
    await switchTo("beta", {
      leave: () => tab.stop(),
      switchWorkspace: () => {
        closedWhenSwitching = opened[0]!.closed;
        return Promise.resolve();
      },
      rejoin: () => tab.start(),
    });
    expect(closedWhenSwitching).toBe(true);
  });
});
