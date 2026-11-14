/**
 * The composer's rules (CP-01, CP-04, CP-06, IL-07): which keys send, the draft, the reply window
 * and the states that stop it. Pure, with an injected clock and storage, so each rule has a test.
 */

export interface KeyLike {
  key: string;
  shiftKey: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
  keyCode?: number;
}

/**
 * Whether this keydown sends. On a pointer-and-keyboard device Enter sends and Shift+Enter makes
 * a new line; on a touch device Enter is a new line and Send sends. Never during an IME
 * composition (Arabic, Turkish, CJK input) - `isComposing` or the legacy 229 - and never on a
 * repeated key.
 */
export function enterSends(event: KeyLike, touch: boolean): boolean {
  if (event.key !== "Enter") return false;
  if (event.isComposing || event.keyCode === 229) return false;
  if (event.repeat) return false;
  if (touch) return false;
  return !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey;
}

/** A touch-first device: no fine pointer that hovers (decided by the pointer media query). */
export function isTouchFirst(matchMedia?: (query: string) => { matches: boolean }): boolean {
  if (!matchMedia) return false;
  return !matchMedia("(hover: hover) and (pointer: fine)").matches;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  key(index: number): string | null;
  readonly length: number;
}

const DRAFT_PREFIX = "wasla-draft:";

/** The draft's key: the workspace and the conversation, nothing else (plan section 21). */
export function draftKey(workspaceId: string, conversationId: string): string {
  return `${DRAFT_PREFIX}${workspaceId}:${conversationId}`;
}

export function readDraft(
  storage: StorageLike | null,
  workspaceId: string,
  conversationId: string,
) {
  try {
    return storage?.getItem(draftKey(workspaceId, conversationId)) ?? "";
  } catch {
    return "";
  }
}

export function writeDraft(
  storage: StorageLike | null,
  workspaceId: string,
  conversationId: string,
  text: string,
): void {
  try {
    if (!storage) return;
    const key = draftKey(workspaceId, conversationId);
    if (text) storage.setItem(key, text);
    else storage.removeItem(key);
  } catch {
    // Private mode or a full store: the draft lives in memory only.
  }
}

/** Sign-out, a workspace switch: every draft of this tab goes (`workspaceId` null = all). */
export function clearDrafts(storage: StorageLike | null, workspaceId: string | null = null): void {
  try {
    if (!storage) return;
    const prefix = workspaceId ? `${DRAFT_PREFIX}${workspaceId}:` : DRAFT_PREFIX;
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(prefix)) keys.push(key);
    }
    for (const key of keys) storage.removeItem(key);
  } catch {
    // Nothing to clear.
  }
}

export interface ConversationFacts {
  status: string;
  mode: string;
  assigned_to_id: string | null;
  reply_policy: {
    free_text_allowed: boolean;
    window_expires_at: string | null;
    out_of_window: string;
    templates: boolean;
    text_limit: number;
    state?: string;
  };
}

export type ComposerMode =
  | { kind: "free"; expiresInMs: number | null }
  | { kind: "templates-only" }
  | { kind: "closed" }
  | { kind: "opted-out" }
  | { kind: "unavailable" };

/** Within this long of the window's end, the composer says how long is left (CP-04). */
export const WINDOW_WARNING_MS = 2 * 60 * 60 * 1000;

export function composerMode(
  conversation: ConversationFacts,
  options: { now: number; optedOut: boolean },
): ComposerMode {
  if (conversation.status === "closed") return { kind: "closed" };
  if (options.optedOut) return { kind: "opted-out" };
  const policy = conversation.reply_policy;
  if (policy.state && policy.state !== "operational") return { kind: "unavailable" };
  if (!policy.free_text_allowed) {
    return policy.templates ? { kind: "templates-only" } : { kind: "unavailable" };
  }
  const ends = policy.window_expires_at ? Date.parse(policy.window_expires_at) : NaN;
  const left = Number.isFinite(ends) ? ends - options.now : null;
  return {
    kind: "free",
    expiresInMs: left !== null && left <= WINDOW_WARNING_MS ? Math.max(0, left) : null,
  };
}

/** IL-07: a member's reply to an unassigned conversation will assign it to them. */
export function replyAssigns(conversation: Pick<ConversationFacts, "assigned_to_id">): boolean {
  return conversation.assigned_to_id === null;
}

/** The counter appears in the last 10 % of the channel's limit (CP-01). */
export function counterVisible(length: number, limit: number): boolean {
  return limit > 0 && length >= Math.floor(limit * 0.9);
}
