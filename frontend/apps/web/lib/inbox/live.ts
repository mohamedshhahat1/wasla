/**
 * The inbox's live rules, beside FE-3's stream (RT-03, RT-04, IL-06, IL-07). Pure decisions with an
 * injected clock; the screens wire them to timers.
 */

/** GT-08's quiet period: the stream may reconnect this long before polling starts. */
export const STREAM_QUIET_MS = 10_000;
/** Plan section 10: the API's poll_after_seconds, else 30 s without a stream. */
export const DEFAULT_POLL_SECONDS = 30;
/** IL-07: a read is sent this long after the newest message comes into view. */
export const READ_DEBOUNCE_MS = 1_000;

export type StreamKind = "connecting" | "open" | "waiting" | "ended" | "stopped";

/**
 * Whether the inbox polls now: only while the stream has been down past its quiet period, and
 * never in a hidden tab.
 */
export function shouldPoll(input: {
  stream: StreamKind;
  since: number;
  now: number;
  visible: boolean;
}): boolean {
  if (!input.visible) return false;
  if (input.stream === "open" || input.stream === "stopped") return false;
  return input.now - input.since >= STREAM_QUIET_MS;
}

export function pollInterval(pollAfterSeconds: number | null | undefined): number {
  const seconds =
    typeof pollAfterSeconds === "number" && pollAfterSeconds > 0
      ? pollAfterSeconds
      : DEFAULT_POLL_SECONDS;
  return seconds * 1000;
}

/**
 * Tracks the stream so the inbox can tell a reconnect (a possible gap, RT-03) from the first open.
 * A stream that opens again after having been open once is treated as a gap: the server replays
 * from lastEventId when it can and sends `resync` when it cannot; reading the open view's first
 * page again costs one request and closes the case where neither arrives.
 */
export class StreamWatch {
  #wasOpen = false;
  #down = false;

  /** Returns true when this state ends a gap. */
  next(kind: StreamKind): boolean {
    if (kind === "open") {
      const gap = this.#wasOpen && this.#down;
      this.#wasOpen = true;
      this.#down = false;
      return gap;
    }
    if (kind === "connecting" || kind === "waiting" || kind === "ended") this.#down = true;
    return false;
  }
}

/**
 * IL-07: whether the open conversation may be marked read now - the tab is visible, the newest
 * loaded message is in view, and it is newer than what the team has already seen.
 */
export function mayMarkRead(input: {
  visible: boolean;
  newestInView: boolean;
  newestMessageId: string | null;
  readMessageId: string | null;
  unreadCount: number;
  alreadySent: string | null;
}): boolean {
  if (!input.visible || !input.newestInView || !input.newestMessageId) return false;
  if (input.alreadySent === input.newestMessageId) return false;
  if (input.readMessageId === input.newestMessageId) return false;
  // Only a customer message nobody has seen moves the team's position; a reply never needs it.
  return input.unreadCount > 0;
}

/**
 * IL-06: a refreshed list is applied at once only when the person is at its top and has no row
 * focused; otherwise the "Updated — show latest" Button waits for them.
 */
export function applyListUpdateNow(input: { scrolledAway: boolean; rowFocused: boolean }): boolean {
  return !input.scrolledAway && !input.rowFocused;
}

/** "99+" above 99 (rows), "999+" above 999 (the Inbox total). */
export function capCount(value: number, cap: number): string {
  return value > cap ? `${cap}+` : String(value);
}
