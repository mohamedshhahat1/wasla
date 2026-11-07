/**
 * Refresh at 80 % of the token's lifetime (plan 8.3), with an injected clock and timer so tests
 * never wait on the wall clock.
 */
import type { TokenHolder, TokenState } from "./token-holder";

export interface Timer {
  now(): number;
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const systemTimer: Timer = {
  now: () => Date.now(),
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export const REFRESH_AT = 0.8;

/** Milliseconds from `now` until the 80 % point of the token's life (0 if already past). */
export function refreshDelay(state: TokenState, now: number): number {
  const lifetime = state.expiresAt - state.issuedAt;
  const due = state.issuedAt + lifetime * REFRESH_AT;
  return Math.max(0, Math.round(due - now));
}

/**
 * Re-arms on every new token from the holder and disarms on sign-out. Returns a stop function.
 */
export function scheduleRefresh(
  holder: TokenHolder,
  onDue: () => void,
  timer: Timer = systemTimer,
): () => void {
  let handle: unknown = null;
  const disarm = () => {
    if (handle !== null) timer.clearTimeout(handle);
    handle = null;
  };
  const arm = (state: TokenState | null) => {
    disarm();
    if (!state) return;
    handle = timer.setTimeout(
      () => {
        handle = null;
        onDue();
      },
      refreshDelay(state, timer.now()),
    );
  };
  arm(holder.state);
  const unsubscribe = holder.subscribe((state) => arm(state));
  return () => {
    unsubscribe();
    disarm();
  };
}
