/**
 * BroadcastChannel "wasla-session" (S-2): one tab's new token or sign-out reaches every tab of
 * the origin. Messages carry the access token (same origin, in memory, never persisted) and
 * never the refresh token, which page script cannot read at all.
 */
import type { TokenHolder, TokenState } from "./token-holder";

export const SESSION_CHANNEL = "wasla-session";

export type SessionMessage =
  | { type: "token-updated"; state: TokenState }
  | { type: "signed-out" }
  /** A tab that just opened asks the others for their valid token (JT-03: no refresh to join). */
  | { type: "token-request" };

/** The part of BroadcastChannel the session uses (a test double implements it). */
export interface ChannelLike {
  postMessage(message: unknown): void;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  close(): void;
}

function isMessage(value: unknown): value is SessionMessage {
  if (typeof value !== "object" || value === null) return false;
  const message = value as { type?: unknown; state?: { accessToken?: unknown } };
  if (message.type === "signed-out" || message.type === "token-request") return true;
  return message.type === "token-updated" && typeof message.state?.accessToken === "string";
}

export class SessionChannel {
  readonly #channel: ChannelLike;
  readonly #holder: TokenHolder;
  readonly #now: () => number;
  readonly #onMessage = (event: MessageEvent) => {
    const data: unknown = event.data;
    if (!isMessage(data)) return;
    if (data.type === "signed-out") this.#holder.clear();
    else if (data.type === "token-request") {
      const state = this.#holder.state;
      if (state && this.#holder.current(this.#now())) this.announceToken(state);
    } else if (data.state.issuedAt >= (this.#holder.state?.issuedAt ?? -Infinity)) {
      this.#holder.set(data.state);
    }
  };

  constructor(holder: TokenHolder, channel: ChannelLike, now: () => number = () => Date.now()) {
    this.#holder = holder;
    this.#channel = channel;
    this.#now = now;
    channel.addEventListener("message", this.#onMessage);
  }

  /** Asks the other tabs for a valid token; one that holds it answers with token-updated. */
  askForToken(): void {
    this.#channel.postMessage({ type: "token-request" } satisfies SessionMessage);
  }

  /** Another tab learns the new token (this tab already holds it). */
  announceToken(state: TokenState): void {
    this.#channel.postMessage({ type: "token-updated", state } satisfies SessionMessage);
  }

  /** Every tab clears its holder, query cache and blob URLs (FE-2 wires the last two). */
  announceSignedOut(): void {
    this.#holder.clear();
    this.#channel.postMessage({ type: "signed-out" } satisfies SessionMessage);
  }

  close(): void {
    this.#channel.removeEventListener("message", this.#onMessage);
    this.#channel.close();
  }
}
