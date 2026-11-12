/** FEF-16: the session's browser primitives, or inert stand-ins where they do not exist. */
import { describe, expect, it, vi } from "vitest";

import { sessionTransports } from "../lib/session/transports";

describe("sessionTransports (FEF-16)", () => {
  it("on a server without BroadcastChannel (workerd) it returns inert transports, no throw", () => {
    const scope = { navigator: {} } as unknown as typeof globalThis;
    const transports = sessionTransports(scope);
    expect(transports.live).toBe(false);
    expect(() => transports.channel.postMessage({ type: "signed-out" })).not.toThrow();
    expect(() => transports.channel.close()).not.toThrow();
  });

  it("on Node's server render (BroadcastChannel defined, no window) it still stays inert", () => {
    const Channel = vi.fn();
    const scope = {
      BroadcastChannel: Channel,
      navigator: { locks: { request: vi.fn() } },
    } as unknown as typeof globalThis;
    expect(sessionTransports(scope).live).toBe(false);
    expect(Channel).not.toHaveBeenCalled();
  });

  it("the inert lock runs the callback once and returns its result", async () => {
    const { locks } = sessionTransports({} as typeof globalThis);
    await expect(locks.request("wasla-refresh", () => Promise.resolve(7))).resolves.toBe(7);
  });

  it("in a browser it opens the wasla-session channel and uses the Web Locks API", () => {
    const opened: string[] = [];
    class Channel {
      constructor(name: string) {
        opened.push(name);
      }
      postMessage() {}
      addEventListener() {}
      removeEventListener() {}
      close() {}
    }
    const locks = { request: vi.fn() };
    const scope = {
      window: {},
      BroadcastChannel: Channel,
      navigator: { locks },
    } as unknown as typeof globalThis;
    const transports = sessionTransports(scope);
    expect(transports.live).toBe(true);
    expect(opened).toEqual(["wasla-session"]);
    expect(transports.locks).toBe(locks);
  });
});
