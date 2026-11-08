/* eslint-disable @typescript-eslint/require-await -- test doubles implement async interfaces without awaiting */
import { describe, expect, it } from "vitest";

import {
  backoffDelay,
  ConnectionLost,
  RefreshCoordinator,
  SessionChannel,
  SignedOut,
  TokenHolder,
  TransientFailure,
  withBackoff,
} from "../src/browser";
import { FakeChannelHub, FakeLocks } from "./doubles";

/** An injected clock whose delay() moves time instantly - nothing here waits. */
function fakeTime(start = 1_000_000) {
  let now = start;
  const waits: number[] = [];
  return {
    clock: { now: () => now },
    delay: async (ms: number) => {
      waits.push(ms);
      now += ms;
    },
    waits,
    advance: (ms: number) => {
      now += ms;
    },
  };
}

describe("backoff (SB-02)", () => {
  it("waits 1, 2, 4 … capped at 30 s", () => {
    expect([1, 2, 3, 4, 5, 6, 7, 8].map(backoffDelay)).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });

  it("retries a network failure while the token is valid, then succeeds", async () => {
    const time = fakeTime();
    let calls = 0;
    const result = await withBackoff(
      async () => {
        calls += 1;
        if (calls < 4) throw new TransientFailure(0);
        return "ok";
      },
      { ...time, expiresAt: () => time.clock.now() + 900_000 },
    );
    expect(result).toBe("ok");
    expect(time.waits).toEqual([1000, 2000, 4000]);
  });

  it("gives up as ConnectionLost once the token would expire during the next wait - not a sign-out", async () => {
    const time = fakeTime();
    const expiresAt = time.clock.now() + 5000;
    await expect(
      withBackoff(
        async () => {
          throw new TransientFailure(503);
        },
        { ...time, expiresAt: () => expiresAt },
      ),
    ).rejects.toBeInstanceOf(ConnectionLost);
    expect(time.waits).toEqual([1000, 2000]);
  });

  it("with no token in hand a network failure is ConnectionLost at once", async () => {
    const time = fakeTime();
    await expect(
      withBackoff(
        async () => {
          throw new TypeError("fetch failed");
        },
        { ...time, expiresAt: () => null },
      ),
    ).rejects.toBeInstanceOf(ConnectionLost);
    expect(time.waits).toEqual([]);
  });

  it("a refusal (401) during backoff ends at once as SignedOut", async () => {
    const time = fakeTime();
    let calls = 0;
    await expect(
      withBackoff(
        async () => {
          calls += 1;
          if (calls === 1) throw new TransientFailure(0);
          throw new SignedOut("unauthenticated");
        },
        { ...time, expiresAt: () => time.clock.now() + 900_000 },
      ),
    ).rejects.toBeInstanceOf(SignedOut);
    expect(calls).toBe(2);
    expect(time.waits).toEqual([1000]);
  });
});

describe("the workspace travels with the token (SB-03)", () => {
  it("a refresh carries active_workspace; every tab adopts it; a switch moves the generation", async () => {
    const locks = new FakeLocks();
    const hub = new FakeChannelHub("sync");
    const tab = () => {
      const holder = new TokenHolder();
      const channel = new SessionChannel(holder, hub.open());
      return { holder, coordinator: new RefreshCoordinator({ holder, locks, channel }) };
    };
    const a = tab();
    const b = tab();
    const acme = { id: "1", name: "Acme", slug: "acme", role: "tenant_owner" };
    const beta = { id: "2", name: "Beta", slug: "beta", role: "member" };
    await a.coordinator.refresh(async () => ({
      access_token: "t1",
      expires_in: 900,
      active_workspace: acme,
    }));
    expect(b.holder.state?.workspace).toEqual(acme);
    expect(b.holder.state?.workspaceSlug).toBe("acme");
    const generation = b.holder.generation;

    let sentSlug: string | null = "unset";
    await a.coordinator.refresh(async (slug) => {
      sentSlug = slug;
      return { access_token: "t2", expires_in: 900, active_workspace: beta };
    });
    expect(sentSlug).toBe("acme");
    expect(b.holder.state?.workspace).toEqual(beta);
    expect(b.holder.generation).toBe(generation + 1);
  });
});
