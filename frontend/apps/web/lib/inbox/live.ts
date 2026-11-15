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

/**
 * RT-04: since when the stream has been down. It is set once when the stream stops being open and
 * kept through its reconnect attempts (connecting ↔ waiting change every few seconds while the API
 * is away); an open stream clears it.
 */
export function downSince(previous: number | null, kind: StreamKind, now: number): number | null {
  if (kind === "open" || kind === "stopped") return null;
  return previous ?? now;
}

export function pollInterval(pollAfterSeconds: number | null | undefined): number {
  const seconds =
    typeof pollAfterSeconds === "number" && pollAfterSeconds > 0
      ? pollAfterSeconds
      : DEFAULT_POLL_SECONDS;
  return seconds * 1000;
}

/**
 * Tracks the stream so the inbox knows when an open may have left a gap (RT-03). A stream that
 * opens again after a drop is one: the server replays from lastEventId when it can and sends
 * `resync` when it cannot; reading the open view's first page again costs one request and closes
 * the case where neither arrives. So is the first open the inbox sees after seeing it closed
 * (FEF-37): the lists were read before it, and a message that arrived between that read and the
 * open has no frame and nothing to replay from. A stream already open when the inbox mounts is
 * not - its frames cover everything after the first read.
 */
export class StreamWatch {
  #down = false;

  /** Returns true when this state ends a gap. */
  next(kind: StreamKind): boolean {
    if (kind === "open") {
      const gap = this.#down;
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
 *
 * `alreadySeen` is the newest message this screen has already had in view, read sent or not
 * (FEF-35): when someone marks the conversation unread, its count rises with no new message, and
 * the screen that was showing it must not read it again - only a newer message does.
 */
export function mayMarkRead(input: {
  visible: boolean;
  newestInView: boolean;
  newestMessageId: string | null;
  readMessageId: string | null;
  unreadCount: number;
  alreadySeen: string | null;
}): boolean {
  if (!input.visible || !input.newestInView || !input.newestMessageId) return false;
  if (input.alreadySeen === input.newestMessageId) return false;
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

/**
 * IL-04 / IL-06: a fetched list that keeps every shown row in place and only adds rows after them
 * is a page the person asked for ("Load more"), never a reorder - it applies at once.
 */
export function onlyAppends(shown: readonly string[], fetched: readonly string[]): boolean {
  return fetched.length > shown.length && shown.every((id, index) => fetched[index] === id);
}

/** "99+" above 99 (rows), "999+" above 999 (the Inbox total). */
export function capCount(value: number, cap: number): string {
  return value > cap ? `${cap}+` : String(value);
}

/**
 * IL-04: whether a failed page read is BE1's "cursor of another view" that the list answers by
 * reading from the top - only for a next page (a cursor was sent), and only once in a row.
 */
export function restartsFromTop(input: {
  code: string | null | undefined;
  cursor: string | null | undefined;
  mismatchesInARow: number;
}): boolean {
  return (
    input.code === "cursor_view_mismatch" && Boolean(input.cursor) && input.mismatchesInARow === 0
  );
}

/** IL-04: the loaded pages cut back to the first one, so a refetch reads from the top only. */
export function firstPageOnly<P>(
  data: { pages: P[]; pageParams: (string | null)[] } | undefined,
): { pages: P[]; pageParams: (string | null)[] } | undefined {
  if (!data || data.pages.length === 0) return data;
  return { pages: data.pages.slice(0, 1), pageParams: [null] };
}
