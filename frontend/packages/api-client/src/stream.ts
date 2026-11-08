/**
 * The event stream from a browser (FX-07, plan section 10, ADR-193). FE-1's PV-03 found that
 * after an API restart EventSource reconnects by itself with the SAME url - the spent ticket -
 * and ends CLOSED. So this helper never relies on EventSource's own reconnect: on any error, or
 * on a `closed` frame whose reason does not end the right to stream, it closes the EventSource
 * and opens a new one with a NEW ticket, after a bounded, jittered backoff (1, 2, 4 … 30 s).
 *
 * Framework-free; time, randomness, fetch and EventSource are injected so tests never wait.
 */
import { CLOSE_REASONS, EVENT_TYPES, type CloseReason } from "./events";

/** Reasons that end this session's right to stream: no reconnect. */
export const TERMINAL_CLOSE_REASONS = [
  "membership_revoked",
  "workspace_switched",
  "workspace_unavailable",
] as const satisfies readonly CloseReason[];

/** Every other reason (server_restart, token_changed): a new ticket and a new EventSource. */
export const RECONNECT_CLOSE_REASONS = CLOSE_REASONS.filter(
  (reason) => !(TERMINAL_CLOSE_REASONS as readonly string[]).includes(reason),
);

export const STREAM_BACKOFF_START_MS = 1000;
export const STREAM_BACKOFF_CAP_MS = 30_000;

export type StreamState =
  | { kind: "connecting"; attempt: number }
  | { kind: "open"; ticketNumber: number }
  | { kind: "waiting"; attempt: number; delayMs: number }
  | { kind: "closed"; reason: string }
  | { kind: "stopped" };

export interface StreamFrameOut {
  type: string;
  id: string | null;
  data: unknown;
}

/** The parts of EventSource the helper uses (a test double implements them). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface StreamTimer {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface OpenEventStreamOptions {
  /** The API origin, e.g. https://api.iwasla.tech. */
  api: string;
  /** A valid access token (the session refreshes if it has to). */
  getAccessToken: () => Promise<string | null>;
  /** After a 401 from the ticket call: one refresh; null when the session is over. */
  onUnauthenticated: () => Promise<string | null>;
  onFrame: (frame: StreamFrameOut) => void;
  onState?: (state: StreamState) => void;
  fetch?: typeof globalThis.fetch;
  eventSource?: EventSourceFactory;
  timer?: StreamTimer;
  random?: () => number;
}

const CLOSED = 2;

/** The wait before reconnect attempt `n` (1-based): 1, 2, 4 … 30 s, jittered to 50–100 %. */
export function streamBackoff(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(
    STREAM_BACKOFF_CAP_MS,
    STREAM_BACKOFF_START_MS * 2 ** Math.max(0, attempt - 1),
  );
  return Math.round(base * (0.5 + 0.5 * random()));
}

const defaultTimer: StreamTimer = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

class TicketRefused extends Error {}

/**
 * Opens the stream and keeps it open by itself. Returns `close()`, which stops everything (the
 * app calls it on sign-out and on a workspace switch - WK-01).
 */
export function openEventStream(options: OpenEventStreamOptions): { close(): void } {
  const doFetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const makeSource: EventSourceFactory = options.eventSource ?? ((url) => new EventSource(url));
  const timer = options.timer ?? defaultTimer;
  const random = options.random ?? Math.random;
  const report = (state: StreamState) => options.onState?.(state);

  let source: EventSourceLike | null = null;
  let pending: unknown = null;
  let stopped = false;
  let attempt = 0;
  let tickets = 0;

  const dropSource = () => {
    if (!source) return;
    source.onopen = null;
    source.onerror = null;
    source.close();
    source = null;
  };

  const finish = (reason: string) => {
    stopped = true;
    dropSource();
    if (pending !== null) timer.clearTimeout(pending);
    pending = null;
    report({ kind: "closed", reason });
  };

  const scheduleReconnect = () => {
    dropSource();
    if (stopped) return;
    attempt += 1;
    const delayMs = streamBackoff(attempt, random);
    report({ kind: "waiting", attempt, delayMs });
    pending = timer.setTimeout(() => {
      pending = null;
      void connect();
    }, delayMs);
  };

  const ticket = async (token: string): Promise<Response> =>
    doFetch(`${options.api}/api/v1/events/ticket`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });

  const takeTicket = async (): Promise<string> => {
    let token = await options.getAccessToken();
    if (!token) throw new TicketRefused();
    let response = await ticket(token);
    if (response.status === 401) {
      token = await options.onUnauthenticated();
      if (!token) throw new TicketRefused();
      response = await ticket(token);
    }
    if (response.status === 401 || response.status === 403) throw new TicketRefused();
    if (!response.ok) throw new Error(`ticket ${response.status}`);
    const body = (await response.json()) as { ticket?: unknown };
    if (typeof body.ticket !== "string" || !body.ticket) throw new Error("no ticket");
    return body.ticket;
  };

  const connect = async () => {
    if (stopped) return;
    report({ kind: "connecting", attempt });
    let value: string;
    try {
      value = await takeTicket();
    } catch (error) {
      if (stopped) return;
      if (error instanceof TicketRefused) finish("unauthenticated");
      else scheduleReconnect();
      return;
    }
    if (stopped) return;
    tickets += 1;
    const opened = makeSource(
      `${options.api}/api/v1/events/stream?ticket=${encodeURIComponent(value)}`,
    );
    source = opened;
    const number = tickets;
    opened.onopen = () => {
      if (source !== opened) return;
      attempt = 0;
      report({ kind: "open", ticketNumber: number });
    };
    // Any error - a non-200 answer (CLOSED) or EventSource starting its own reconnect with the
    // spent ticket (CONNECTING) - ends this EventSource; the helper opens its own.
    opened.onerror = () => {
      if (source !== opened) return;
      scheduleReconnect();
    };
    for (const type of EVENT_TYPES) {
      opened.addEventListener(type, (event) => {
        if (source !== opened) return;
        options.onFrame({ type, id: event.lastEventId || null, data: parse(event.data) });
      });
    }
    opened.addEventListener("resync", (event) => {
      if (source !== opened) return;
      options.onFrame({ type: "resync", id: event.lastEventId || null, data: parse(event.data) });
    });
    opened.addEventListener("closed", (event) => {
      if (source !== opened) return;
      const data = parse(event.data) as { reason?: unknown } | null;
      const reason = typeof data?.reason === "string" ? data.reason : "unknown";
      options.onFrame({ type: "closed", id: event.lastEventId || null, data });
      if ((TERMINAL_CLOSE_REASONS as readonly string[]).includes(reason)) finish(reason);
      else scheduleReconnect();
    });
    if (opened.readyState === CLOSED) scheduleReconnect();
  };

  void connect();
  return {
    close() {
      if (stopped) return;
      stopped = true;
      dropSource();
      if (pending !== null) timer.clearTimeout(pending);
      pending = null;
      report({ kind: "stopped" });
    },
  };
}

function parse(data: unknown): unknown {
  if (typeof data !== "string" || !data) return null;
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}
