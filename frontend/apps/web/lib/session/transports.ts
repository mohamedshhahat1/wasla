/**
 * The browser primitives the session needs - BroadcastChannel and the Web Locks API - or inert
 * stand-ins where they do not exist. The provider's session is also constructed while Next.js
 * renders on the server; Cloudflare's workerd has no BroadcastChannel (Node 24 does, which is why
 * `next start` never failed), and constructing one threw on every page (FEF-16). The server render
 * never restores or refreshes - that runs in an effect, in the browser - so the stand-ins are never
 * asked to carry anything.
 */
import { SESSION_CHANNEL, type ChannelLike, type LockManagerLike } from "@wasla/session";

const inertChannel: ChannelLike = {
  postMessage: () => undefined,
  addEventListener: () => undefined,
  removeEventListener: () => undefined,
  close: () => undefined,
};

const inertLocks: LockManagerLike = {
  request: (_name, callback) => callback(),
};

export interface SessionTransports {
  channel: ChannelLike;
  locks: LockManagerLike;
  /** False on the server (or an engine without the APIs): the session must not refresh there. */
  live: boolean;
}

export function sessionTransports(scope: typeof globalThis = globalThis): SessionTransports {
  const Channel = (scope as { BroadcastChannel?: typeof BroadcastChannel }).BroadcastChannel;
  const locks = (scope as { navigator?: Navigator }).navigator?.locks;
  const browser = typeof (scope as { window?: unknown }).window !== "undefined";
  if (browser && typeof Channel === "function" && locks) {
    return { channel: new Channel(SESSION_CHANNEL), locks, live: true };
  }
  return { channel: inertChannel, locks: inertLocks, live: false };
}
