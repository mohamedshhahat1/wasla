/**
 * Test doubles for Web Locks and BroadcastChannel (SS-02). One `Browser` is one origin in one
 * browser: its tabs share a lock manager and a channel hub, as real tabs do.
 *
 * Ordering modelled: a channel message is delivered to the other tabs before the lock is
 * granted to the next waiter (delivery is synchronous; a grant waits a microtask). The real
 * browsers' order is measured by the Playwright test, not assumed.
 */
import type { ChannelLike, LockManagerLike } from "../src/browser";

export class FakeLocks implements LockManagerLike {
  readonly #queues = new Map<string, Array<() => void>>();
  readonly #held = new Set<string>();
  granted = 0;
  readonly grantsByName = new Map<string, number>();

  query(): Promise<{ held: Array<{ name: string }> }> {
    return Promise.resolve({ held: [...this.#held].map((name) => ({ name })) });
  }

  async request<T>(name: string, callback: () => Promise<T>): Promise<T> {
    if (this.#held.has(name)) {
      await new Promise<void>((resolve) => {
        const queue = this.#queues.get(name) ?? [];
        queue.push(resolve);
        this.#queues.set(name, queue);
      });
    }
    this.#held.add(name);
    this.granted += 1;
    this.grantsByName.set(name, (this.grantsByName.get(name) ?? 0) + 1);
    try {
      return await callback();
    } finally {
      const next = this.#queues.get(name)?.shift();
      if (next) queueMicrotask(next);
      else this.#held.delete(name);
    }
  }
}

export class FakeChannelHub {
  readonly #channels = new Set<FakeChannel>();
  /**
   * "sync": delivered before the next lock grant. "late": delivered a macrotask later, after
   * the grant - the order Chromium was seen to use. "lost": never (the sending tab closed).
   */
  constructor(readonly delivery: "sync" | "late" | "lost" = "sync") {}
  open(): FakeChannel {
    const channel = new FakeChannel(this);
    this.#channels.add(channel);
    return channel;
  }
  deliver(from: FakeChannel, data: unknown): void {
    if (this.delivery === "lost") return;
    const send = () => {
      for (const channel of this.#channels) if (channel !== from) channel.receive(data);
    };
    if (this.delivery === "late") setTimeout(send, 0);
    else send();
  }
  remove(channel: FakeChannel): void {
    this.#channels.delete(channel);
  }
}

export class FakeChannel implements ChannelLike {
  readonly #listeners = new Set<(event: MessageEvent) => void>();
  readonly #hub: FakeChannelHub;
  constructor(hub: FakeChannelHub) {
    this.#hub = hub;
  }
  postMessage(message: unknown): void {
    this.#hub.deliver(this, structuredClone(message));
  }
  addEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.#listeners.add(listener);
  }
  removeEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.#listeners.delete(listener);
  }
  receive(data: unknown): void {
    for (const listener of this.#listeners) listener({ data } as MessageEvent);
  }
  close(): void {
    this.#hub.remove(this);
  }
}
