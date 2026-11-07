/**
 * The browser half of S-2 (ADR-189, SS-01): framework-free, nothing persistent, no API call of
 * its own - FE-2 injects the /session/refresh call as `fn`.
 */
export {
  HANDOFF_TIMEOUT_MS,
  REFRESH_LOCK,
  RefreshCoordinator,
  TOKEN_MARKER_PREFIX,
  systemClock,
  type Clock,
  type LockManagerLike,
  type RefreshCoordinatorOptions,
  type RefreshResult,
} from "./refresh-coordinator";
export { REFRESH_AT, refreshDelay, scheduleRefresh, systemTimer, type Timer } from "./scheduler";
export {
  SESSION_CHANNEL,
  SessionChannel,
  type ChannelLike,
  type SessionMessage,
} from "./session-channel";
export { TokenHolder, type HolderListener, type TokenState } from "./token-holder";

import { RefreshCoordinator } from "./refresh-coordinator";
import { SESSION_CHANNEL, SessionChannel } from "./session-channel";
import { TokenHolder } from "./token-holder";

/** Wires the three together on the real browser APIs (Web Locks, BroadcastChannel). */
export function createBrowserSession() {
  const holder = new TokenHolder();
  const channel = new SessionChannel(holder, new BroadcastChannel(SESSION_CHANNEL));
  const coordinator = new RefreshCoordinator({ holder, locks: navigator.locks, channel });
  return { holder, channel, coordinator };
}
