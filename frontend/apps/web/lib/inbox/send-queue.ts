/**
 * Optimistic sends (CP-02). Each bubble gets one client id and one Idempotency-Key, made once and
 * reused for every retry of that bubble (plan section 14: "the same message id … proved not to
 * duplicate"). A 201, a `message.created` frame or a refetch that carries the server's message id
 * settles the bubble; the same id never draws twice. Sends of one conversation go in order.
 * Nothing here is stored: a reload drops unsent bubbles (their text goes back to the draft).
 */

export type PendingKind = "text" | "template" | "media";

export type PendingStatus = "sending" | "failed";

export interface PendingSend {
  clientId: string;
  idempotencyKey: string;
  conversationId: string;
  kind: PendingKind;
  /** The text the bubble shows while it is sending (text, a caption, a template's preview). */
  text: string;
  /** For a file: its name, shown while uploading. */
  fileName?: string;
  status: PendingStatus;
  /** The error code of the last failure, for its translated sentence. */
  errorCode?: string;
  /** Whether Retry can help (never for an opt-out or a closed window). */
  retryable?: boolean;
  /** Set once the server answered with its message. */
  messageId?: string;
  createdAt: number;
}

/** Codes a retry cannot fix (CP-02, CP-04, CP-06). */
export const FINAL_CODES: ReadonlySet<string> = new Set([
  "recipient_opted_out",
  "suppressed",
  "reply_window_closed",
  "template_refused",
  "marketing_template_admin_only",
  "media_type_not_allowed",
  "payload_too_large",
  "validation_error",
  "permission_denied",
  "not_found",
  "workspace_suspended",
  "membership_revoked",
]);

export function isRetryable(status: number | null, code: string | null): boolean {
  if (code && FINAL_CODES.has(code)) return false;
  if (status === null) return true; // a network error or a timeout
  if (status >= 500 || status === 429 || status === 408) return true;
  return code === "retryable_conflict" || code === "rate_limited" || code === "network_error";
}

export interface QueueDeps {
  newId: () => string;
  now: () => number;
}

export const DEFAULT_DEPS: QueueDeps = {
  newId: () => crypto.randomUUID(),
  now: () => Date.now(),
};

type Listener = () => void;

/** One per tab: every unsettled bubble of every conversation, and the key → message id map. */
export class SendQueue {
  #pending = new Map<string, PendingSend>();
  #settled = new Map<string, string>();
  #listeners = new Set<Listener>();
  #deps: QueueDeps;
  #chains = new Map<string, Promise<unknown>>();

  constructor(deps: QueueDeps = DEFAULT_DEPS) {
    this.#deps = deps;
  }

  subscribe(listener: Listener): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #emit(): void {
    for (const listener of this.#listeners) listener();
  }

  /** A new bubble with its own key. */
  add(
    input: Omit<PendingSend, "clientId" | "idempotencyKey" | "status" | "createdAt">,
  ): PendingSend {
    const send: PendingSend = {
      ...input,
      clientId: this.#deps.newId(),
      idempotencyKey: this.#deps.newId(),
      status: "sending",
      createdAt: this.#deps.now(),
    };
    this.#pending.set(send.clientId, send);
    this.#emit();
    return send;
  }

  get(clientId: string): PendingSend | undefined {
    return this.#pending.get(clientId);
  }

  /** The bubbles still drawn for a conversation, oldest first. */
  forConversation(conversationId: string): PendingSend[] {
    return [...this.#pending.values()]
      .filter((send) => send.conversationId === conversationId)
      .sort((a, b) => a.createdAt - b.createdAt);
  }

  /** Runs `job` after the conversation's earlier sends have answered (in order, never parallel). */
  enqueue<T>(conversationId: string, job: () => Promise<T>): Promise<T> {
    const previous = this.#chains.get(conversationId) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(job);
    this.#chains.set(conversationId, next);
    return next;
  }

  /** The server answered with `messageId`: the bubble becomes that message. */
  settle(clientId: string, messageId: string): void {
    const send = this.#pending.get(clientId);
    if (!send) return;
    this.#settled.set(send.idempotencyKey, messageId);
    this.#pending.delete(clientId);
    this.#emit();
  }

  fail(clientId: string, failure: { code: string; retryable: boolean }): void {
    const send = this.#pending.get(clientId);
    if (!send) return;
    this.#pending.set(clientId, {
      ...send,
      status: "failed",
      errorCode: failure.code,
      retryable: failure.retryable,
    });
    this.#emit();
  }

  /** Retry: the same bubble, the same key. */
  retry(clientId: string): PendingSend | undefined {
    const send = this.#pending.get(clientId);
    if (!send) return undefined;
    const again: PendingSend = { ...send, status: "sending" };
    delete again.errorCode;
    delete again.retryable;
    this.#pending.set(clientId, again);
    this.#emit();
    return again;
  }

  discard(clientId: string): void {
    if (this.#pending.delete(clientId)) this.#emit();
  }

  /** Message ids the server gave to this tab's sends (a frame or refetch with one draws nothing new). */
  settledIds(): ReadonlySet<string> {
    return new Set(this.#settled.values());
  }

  /** A loaded page now holds `messageIds`: any bubble those ids settle disappears. */
  reconcile(messageIds: Iterable<string>): void {
    const ids = new Set(messageIds);
    let changed = false;
    for (const [clientId, send] of this.#pending) {
      if (send.messageId && ids.has(send.messageId)) {
        this.#pending.delete(clientId);
        changed = true;
      }
    }
    if (changed) this.#emit();
  }

  /** Sign-out, a switch: nothing of the session stays. */
  clear(): void {
    this.#pending.clear();
    this.#settled.clear();
    this.#chains.clear();
    this.#emit();
  }
}

/**
 * Merge loaded messages and pending bubbles without a duplicate (CP-02): a message id appears once,
 * whichever of the 201, the frame or a refetch brought it.
 */
export function dedupeById<T extends { id: string }>(messages: readonly T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const message of messages) {
    if (seen.has(message.id)) continue;
    seen.add(message.id);
    out.push(message);
  }
  return out;
}
