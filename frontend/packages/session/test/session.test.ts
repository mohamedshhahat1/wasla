import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  RefreshCoordinator,
  refreshDelay,
  scheduleRefresh,
  SessionChannel,
  TokenHolder,
  type Clock,
  type RefreshResult,
  type Timer,
} from "../src/browser";
import { canonicalString, forwardPath, signForward } from "../src/server";
import { FakeChannelHub, FakeLocks } from "./doubles";

const here = dirname(fileURLToPath(import.meta.url));

const T0 = Date.UTC(2026, 10, 7, 12, 0, 0);
const fixedClock = (start = T0): Clock & { advance(ms: number): void } => {
  let now = start;
  return { now: () => now, advance: (ms) => (now += ms) };
};

function browserWithTabs(
  count: number,
  clock: Clock,
  delivery: "sync" | "late" | "lost" = "sync",
  delay: (ms: number) => Promise<void> = () => new Promise(() => undefined),
) {
  const locks = new FakeLocks();
  const hub = new FakeChannelHub(delivery);
  const tabs = Array.from({ length: count }, () => {
    const holder = new TokenHolder();
    const channel = new SessionChannel(holder, hub.open());
    const coordinator = new RefreshCoordinator({ holder, locks, channel, clock, delay });
    return { holder, channel, coordinator };
  });
  return { locks, tabs };
}

describe("one refresh per browser (SS-02, the browser half of J6)", () => {
  it("50 concurrent calls across 5 tabs run fn once and all get its token", async () => {
    const clock = fixedClock();
    const { tabs } = browserWithTabs(5, clock);
    let runs = 0;
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      return { access_token: `access-${runs}`, expires_in: 900, workspace_slug: "acme" };
    };
    const calls = Array.from({ length: 50 }, (_, i) => tabs[i % 5]!.coordinator.refresh(fn));
    const tokens = await Promise.all(calls);
    expect(runs).toBe(1);
    expect(new Set(tokens)).toEqual(new Set(["access-1"]));
    for (const tab of tabs) expect(tab.holder.current(clock.now())).toBe("access-1");
  });

  it("still once when the broadcast arrives after the lock grant (the marker hand-off)", async () => {
    const clock = fixedClock();
    const { tabs } = browserWithTabs(5, clock, "late");
    let runs = 0;
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      return { access_token: `access-${runs}`, expires_in: 900 };
    };
    for (let round = 1; round <= 5; round += 1) {
      clock.advance(1000);
      const tokens = await Promise.all(
        Array.from({ length: 20 }, (_, i) => tabs[i % 5]!.coordinator.refresh(fn)),
      );
      expect(runs, `round ${round}`).toBe(round);
      expect(new Set(tokens)).toEqual(new Set([`access-${round}`]));
    }
  });

  it("without the marker, a late broadcast makes the waiting tabs refresh again (why it exists)", async () => {
    const clock = fixedClock();
    const locks = new FakeLocks();
    const noQuery = { request: locks.request.bind(locks) };
    const hub = new FakeChannelHub("late");
    const tabs = Array.from({ length: 3 }, () => {
      const holder = new TokenHolder();
      return new RefreshCoordinator({
        holder,
        locks: noQuery,
        channel: new SessionChannel(holder, hub.open()),
        clock,
      });
    });
    let runs = 0;
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      return { access_token: `access-${runs}`, expires_in: 900 };
    };
    await Promise.all(tabs.map((tab) => tab.refresh(fn)));
    expect(runs).toBeGreaterThan(1);
  });

  it("if the announced token never arrives (its tab closed), the waiter refreshes after the timeout", async () => {
    const clock = fixedClock();
    let timeouts = 0;
    const immediately = () => {
      timeouts += 1;
      return Promise.resolve();
    };
    const { tabs } = browserWithTabs(2, clock, "lost", immediately);
    let runs = 0;
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      return { access_token: `access-${runs}`, expires_in: 900 };
    };
    const [a, b] = await Promise.all([
      tabs[0]!.coordinator.refresh(fn),
      tabs[1]!.coordinator.refresh(fn),
    ]);
    expect(runs).toBe(2);
    expect(timeouts).toBe(1);
    expect([a, b].sort()).toEqual(["access-1", "access-2"]);
  });

  it("without the lock the same 50 calls would refresh five times (the double is not trivial)", async () => {
    const clock = fixedClock();
    let runs = 0;
    const noLock = { request: <T>(_name: string, callback: () => Promise<T>) => callback() };
    const hub = new FakeChannelHub();
    const tabs = Array.from({ length: 5 }, () => {
      const holder = new TokenHolder();
      return new RefreshCoordinator({
        holder,
        locks: noLock,
        channel: new SessionChannel(holder, hub.open()),
        clock,
      });
    });
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      return { access_token: `access-${runs}`, expires_in: 900 };
    };
    await Promise.all(Array.from({ length: 50 }, (_, i) => tabs[i % 5]!.refresh(fn)));
    expect(runs).toBe(5);
  });

  it("a failed refresh releases the lock and the next caller retries once", async () => {
    const clock = fixedClock();
    const { tabs, locks } = browserWithTabs(5, clock);
    let runs = 0;
    const fn = async (): Promise<RefreshResult> => {
      runs += 1;
      await Promise.resolve();
      if (runs === 1) throw new Error("network");
      return { access_token: "access-2", expires_in: 900 };
    };
    const results = await Promise.allSettled(
      Array.from({ length: 50 }, (_, i) => tabs[i % 5]!.coordinator.refresh(fn)),
    );
    expect(runs).toBe(2);
    expect(results.filter((r) => r.status === "rejected")).toHaveLength(10);
    expect(
      results.filter((r) => r.status === "fulfilled").every((r) => r.value === "access-2"),
    ).toBe(true);
    expect(locks.grantsByName.get("wasla-refresh")).toBe(5);
  });

  it("sends the current workspace with the refresh (plan 8.1)", async () => {
    const clock = fixedClock();
    const { tabs } = browserWithTabs(1, clock);
    const tab = tabs[0]!;
    tab.holder.set({ accessToken: "a", issuedAt: T0, expiresAt: T0 + 1000, workspaceSlug: "beta" });
    const fn = vi.fn(() => Promise.resolve({ access_token: "b", expires_in: 900 }));
    await tab.coordinator.refresh(fn);
    expect(fn).toHaveBeenCalledWith("beta");
    expect(tab.holder.state?.workspaceSlug).toBe("beta");
  });

  it("refuses an answer without a usable token", async () => {
    const { tabs } = browserWithTabs(1, fixedClock());
    await expect(
      tabs[0]!.coordinator.refresh(() => Promise.resolve({ access_token: "", expires_in: 900 })),
    ).rejects.toThrow();
  });
});

describe("the session channel", () => {
  it("signed-out in one tab clears every holder and bumps every generation", () => {
    const { tabs } = browserWithTabs(3, fixedClock());
    for (const tab of tabs) {
      tab.holder.set({
        accessToken: "a",
        issuedAt: T0,
        expiresAt: T0 + 900_000,
        workspaceSlug: "w",
      });
    }
    const before = tabs.map((tab) => tab.holder.generation);
    tabs[1]!.channel.announceSignedOut();
    tabs.forEach((tab, i) => {
      expect(tab.holder.state).toBeNull();
      expect(tab.holder.generation).toBe(before[i]! + 1);
    });
  });

  it("a tab that just opened asks; a tab with a valid token hands it over (JT-03)", () => {
    const hub = new FakeChannelHub();
    const holding = new TokenHolder();
    new SessionChannel(holding, hub.open(), () => T0);
    holding.set({ accessToken: "held", issuedAt: T0, expiresAt: T0 + 900_000, workspaceSlug: "w" });
    const joining = new TokenHolder();
    const channel = new SessionChannel(joining, hub.open(), () => T0);
    channel.askForToken();
    expect(joining.current(T0)).toBe("held");
    expect(joining.state?.workspaceSlug).toBe("w");
  });

  it("an expired token is not handed over, and a tab without one stays silent", () => {
    const hub = new FakeChannelHub();
    const expired = new TokenHolder();
    new SessionChannel(expired, hub.open(), () => T0 + 1_000_000);
    expired.set({ accessToken: "old", issuedAt: T0, expiresAt: T0 + 900_000, workspaceSlug: null });
    new SessionChannel(new TokenHolder(), hub.open(), () => T0);
    const joining = new TokenHolder();
    new SessionChannel(joining, hub.open(), () => T0).askForToken();
    expect(joining.state).toBeNull();
  });

  it("a workspace switch bumps the generation; a plain refresh does not", () => {
    const holder = new TokenHolder();
    holder.set({ accessToken: "a", issuedAt: T0, expiresAt: T0 + 1, workspaceSlug: "w1" });
    const g = holder.generation;
    holder.set({ accessToken: "b", issuedAt: T0 + 1, expiresAt: T0 + 2, workspaceSlug: "w1" });
    expect(holder.generation).toBe(g);
    holder.set({ accessToken: "c", issuedAt: T0 + 2, expiresAt: T0 + 3, workspaceSlug: "w2" });
    expect(holder.generation).toBe(g + 1);
  });

  it("ignores an older token and foreign messages", () => {
    const hub = new FakeChannelHub();
    const holder = new TokenHolder();
    new SessionChannel(holder, hub.open());
    const other = hub.open();
    holder.set({
      accessToken: "new",
      issuedAt: T0 + 10,
      expiresAt: T0 + 900_000,
      workspaceSlug: null,
    });
    other.postMessage({
      type: "token-updated",
      state: { accessToken: "old", issuedAt: T0, expiresAt: T0 + 900_000, workspaceSlug: null },
    });
    other.postMessage({ type: "something-else" });
    other.postMessage("noise");
    expect(holder.state?.accessToken).toBe("new");
  });
});

describe("refresh at 80 % of the lifetime, on an injected clock", () => {
  let pending: Array<{ at: number; callback: () => void; id: number }> = [];
  let now = T0;
  let nextId = 0;
  const timer: Timer = {
    now: () => now,
    setTimeout: (callback, ms) => {
      const id = ++nextId;
      pending.push({ at: now + ms, callback, id });
      return id;
    },
    clearTimeout: (handle) => {
      pending = pending.filter((entry) => entry.id !== handle);
    },
  };
  const advanceTo = (time: number) => {
    now = time;
    for (const entry of pending.filter((e) => e.at <= time)) {
      pending = pending.filter((e) => e !== entry);
      entry.callback();
    }
  };
  afterEach(() => {
    pending = [];
    now = T0;
  });

  it("computes the delay", () => {
    const state = { accessToken: "a", issuedAt: T0, expiresAt: T0 + 900_000, workspaceSlug: null };
    expect(refreshDelay(state, T0)).toBe(720_000);
    expect(refreshDelay(state, T0 + 700_000)).toBe(20_000);
    expect(refreshDelay(state, T0 + 800_000)).toBe(0);
  });

  it("fires once at 720 s of a 900 s token, re-arms on a new token, stops on sign-out", () => {
    const holder = new TokenHolder();
    const due = vi.fn();
    const stop = scheduleRefresh(holder, due, timer);
    holder.set({ accessToken: "a", issuedAt: T0, expiresAt: T0 + 900_000, workspaceSlug: null });
    advanceTo(T0 + 719_999);
    expect(due).not.toHaveBeenCalled();
    advanceTo(T0 + 720_000);
    expect(due).toHaveBeenCalledTimes(1);
    holder.set({
      accessToken: "b",
      issuedAt: T0 + 720_000,
      expiresAt: T0 + 1_620_000,
      workspaceSlug: null,
    });
    holder.clear();
    advanceTo(T0 + 2_000_000);
    expect(due).toHaveBeenCalledTimes(1);
    stop();
    expect(pending).toEqual([]);
  });
});

describe("the forward signer equals FE-0's vectors (SS-03)", () => {
  const vectors = JSON.parse(
    readFileSync(
      join(here, "..", "..", "api-client", "fixtures", "forward-signature.v1.json"),
      "utf8",
    ),
  ) as {
    headers: { client_ip: string; ts: string; signature: string };
    cases: Array<{
      name: string;
      secret: string;
      ts: string;
      client_ip: string;
      method: string;
      path: string;
      wire_path: string;
      canonical: string;
      signature: string;
    }>;
  };

  it("has FE-0's seven cases", () => {
    expect(vectors.cases).toHaveLength(7);
  });

  for (const c of vectors.cases) {
    it(`signs "${c.name}" exactly`, () => {
      expect(canonicalString(c.ts, c.client_ip, c.method, c.path)).toBe(c.canonical);
      const now = Number(c.ts) * 1000 + 999;
      const headers = signForward({
        secret: c.secret,
        clientIp: c.client_ip,
        method: c.method.toLocaleLowerCase("en"),
        path: forwardPath(c.wire_path),
        now,
      });
      expect(headers).toEqual({
        [vectors.headers.client_ip]: c.client_ip,
        [vectors.headers.ts]: c.ts,
        [vectors.headers.signature]: c.signature,
      });
    });
  }

  it("a different secret, address, method, path or second changes the signature", () => {
    const c = vectors.cases[0]!;
    const base = {
      secret: c.secret,
      clientIp: c.client_ip,
      method: c.method,
      path: c.path,
      now: Number(c.ts) * 1000,
    };
    const sig = (input: typeof base) => signForward(input)["X-Wasla-Forward-Sig"];
    const original = sig(base);
    expect(original).toBe(c.signature);
    for (const changed of [
      { ...base, secret: `${c.secret}x` },
      { ...base, clientIp: "203.0.113.8" },
      { ...base, method: "GET" },
      { ...base, path: "/api/v1/auth/refresh" },
      { ...base, now: base.now + 1000 },
    ]) {
      expect(sig(changed)).not.toBe(original);
    }
  });

  it("refuses a query in the path, an empty secret and a spaced address", () => {
    const ok = {
      secret: "s".repeat(32),
      clientIp: "192.0.2.1",
      method: "POST",
      path: "/a",
      now: T0,
    };
    expect(() => signForward({ ...ok, path: "/a?x=1" })).toThrow();
    expect(() => signForward({ ...ok, secret: "" })).toThrow();
    expect(() => signForward({ ...ok, clientIp: "192.0.2.1 " })).toThrow();
    expect(forwardPath("https://api.example/api/v1/auth/x%20y?next=1")).toBe("/api/v1/auth/x y");
  });
});
