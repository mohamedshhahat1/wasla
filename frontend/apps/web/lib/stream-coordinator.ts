/**
 * One realtime stream per browser per workspace (FE-3, ST-01, PD-14, plan section 10).
 *
 * Every tab of the workspace asks for the Web Lock `wasla-stream-<workspace id>`. The tab that
 * holds it is the leader: it opens the stream with FE-2's helper (`openEventStream`, unchanged -
 * new ticket per connection, backoff 1, 2, 4 … 30 s, terminal reasons end it) and posts every frame
 * and every state change on the BroadcastChannel of the same name. The other tabs are followers
 * and apply those messages as if they had their own stream. When the leader's tab closes, the lock
 * passes to the next tab in its queue, which opens a new stream with a new ticket and the last
 * event id it saw (`lastEventId` - the query alias the route accepts, because a new EventSource
 * cannot set the header).
 *
 * Framework-free and stored nowhere: no frame, id or ticket touches any storage. Locks, the
 * channel and the stream opener are injected, so the unit tests run without a browser.
 */
import type { StreamFrameOut, StreamState } from "@wasla/api-client/stream";

/** The Web Locks call the coordinator makes (navigator.locks satisfies it). */
export interface StreamLocks {
  request(
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<void>,
  ): Promise<void>;
}

export interface StreamChannel {
  postMessage(message: unknown): void;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  close(): void;
}

export interface OpenStreamHandlers {
  onFrame: (frame: StreamFrameOut) => void;
  onState: (state: StreamState) => void;
}

/** Opens one stream (the app wires FE-2's openEventStream here); returns its close(). */
export type StreamOpener = (
  handlers: OpenStreamHandlers,
  lastEventId: string | null,
) => { close(): void };

export interface CoordinatorDeps {
  locks: StreamLocks;
  channel: (name: string) => StreamChannel;
  open: StreamOpener;
}

export type Role = "waiting-for-lock" | "leader" | "follower" | "stopped";

/** What every tab shows: the leader's stream state, shared on the channel (ST-06). */
export type SharedState =
  | { kind: "connecting" }
  | { kind: "open" }
  | { kind: "waiting"; attempt: number }
  | { kind: "ended"; reason: string }
  | { kind: "stopped" };

type Message =
  | { type: "frame"; frame: StreamFrameOut }
  | { type: "state"; state: SharedState }
  | { type: "hello" }
  | { type: "reconnect" };

function isMessage(value: unknown): value is Message {
  const type = (value as { type?: unknown } | null)?.type;
  return type === "frame" || type === "state" || type === "hello" || type === "reconnect";
}

export function streamLockName(workspaceId: string): string {
  return `wasla-stream-${workspaceId}`;
}

export function toShared(state: StreamState): SharedState {
  switch (state.kind) {
    case "connecting":
      return { kind: "connecting" };
    case "open":
      return { kind: "open" };
    case "waiting":
      return { kind: "waiting", attempt: state.attempt };
    case "closed":
      return { kind: "ended", reason: state.reason };
    case "stopped":
      return { kind: "stopped" };
  }
}

export class StreamCoordinator {
  readonly name: string;
  #deps: CoordinatorDeps;
  #channel: StreamChannel | null = null;
  #abort: AbortController | null = null;
  #releaseLock: (() => void) | null = null;
  #stream: { close(): void } | null = null;
  #role: Role = "stopped";
  #state: SharedState = { kind: "connecting" };
  #lastEventId: string | null = null;
  #frameListeners = new Set<(frame: StreamFrameOut) => void>();
  #stateListeners = new Set<(state: SharedState, role: Role) => void>();
  #onMessage = (event: MessageEvent) => this.#receive(event.data);

  constructor(workspaceId: string, deps: CoordinatorDeps) {
    if (!workspaceId) throw new Error("a stream coordinator needs a workspace id");
    this.name = streamLockName(workspaceId);
    this.#deps = deps;
  }

  get role(): Role {
    return this.#role;
  }

  get state(): SharedState {
    return this.#state;
  }

  get lastEventId(): string | null {
    return this.#lastEventId;
  }

  onFrame(listener: (frame: StreamFrameOut) => void): () => void {
    this.#frameListeners.add(listener);
    return () => this.#frameListeners.delete(listener);
  }

  onState(listener: (state: SharedState, role: Role) => void): () => void {
    this.#stateListeners.add(listener);
    return () => this.#stateListeners.delete(listener);
  }

  /** Joins the workspace's stream: follower at once, leader when the lock is granted. */
  start(): void {
    if (this.#role !== "stopped") return;
    this.#channel = this.#deps.channel(this.name);
    this.#channel.addEventListener("message", this.#onMessage);
    this.#role = "waiting-for-lock";
    this.#setState({ kind: "connecting" }, false);
    // A follower asks the leader (if any) for its state.
    this.#channel.postMessage({ type: "hello" } satisfies Message);
    const abort = new AbortController();
    this.#abort = abort;
    this.#deps.locks
      .request(this.name, { signal: abort.signal }, () => {
        if (abort.signal.aborted) return Promise.resolve();
        return new Promise<void>((release) => {
          this.#releaseLock = release;
          this.#lead();
        });
      })
      .catch(() => undefined);
  }

  /**
   * Leaves the stream (sign-out, a switch, a gate): the stream closed, the lock released or the
   * wait for it abandoned, the channel closed. Synchronous, so a switch can close the old
   * workspace's stream before it adopts the new token (ST-05).
   */
  stop(): void {
    if (this.#role === "stopped") return;
    this.#role = "stopped";
    this.#stream?.close();
    this.#stream = null;
    this.#abort?.abort();
    this.#abort = null;
    this.#releaseLock?.();
    this.#releaseLock = null;
    this.#channel?.removeEventListener("message", this.#onMessage);
    this.#channel?.close();
    this.#channel = null;
    this.#setState({ kind: "stopped" }, false);
  }

  /** "Reconnect here" / "Try again" (ST-03): the leader opens once more; a follower asks it to. */
  reconnect(): void {
    if (this.#role === "leader") this.#openStream();
    else if (this.#role !== "stopped")
      this.#channel?.postMessage({ type: "reconnect" } satisfies Message);
  }

  #lead(): void {
    if (this.#role === "stopped") {
      this.#releaseLock?.();
      return;
    }
    this.#role = "leader";
    this.#openStream();
  }

  #openStream(): void {
    this.#stream?.close();
    this.#stream = this.#deps.open(
      {
        onFrame: (frame) => {
          if (this.#role !== "leader") return;
          this.#deliver(frame);
          this.#channel?.postMessage({ type: "frame", frame } satisfies Message);
        },
        onState: (state) => {
          if (this.#role !== "leader") return;
          this.#setState(toShared(state), true);
        },
      },
      this.#lastEventId,
    );
  }

  #deliver(frame: StreamFrameOut): void {
    if (frame.id) this.#lastEventId = frame.id;
    for (const listener of this.#frameListeners) listener(frame);
  }

  #setState(state: SharedState, broadcast: boolean): void {
    this.#state = state;
    if (broadcast) this.#channel?.postMessage({ type: "state", state } satisfies Message);
    for (const listener of this.#stateListeners) listener(state, this.#role);
  }

  #receive(data: unknown): void {
    if (!isMessage(data) || this.#role === "stopped") return;
    switch (data.type) {
      case "frame":
        if (this.#role !== "leader") this.#deliver(data.frame);
        return;
      case "state":
        if (this.#role !== "leader") this.#setState(data.state, false);
        return;
      case "hello":
        if (this.#role === "leader") {
          this.#channel?.postMessage({ type: "state", state: this.#state } satisfies Message);
        }
        return;
      case "reconnect":
        if (this.#role === "leader") this.#openStream();
        return;
    }
  }
}
