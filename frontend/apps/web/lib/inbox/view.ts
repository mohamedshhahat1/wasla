/**
 * The inbox's view and its place in the URL (IL-02, LY-03, UR-09). The view maps one to one to the
 * API's parameters (BE1's FEB-05 and UR's `unread`); nothing is invented. The URL carries the view,
 * the open conversation's id and the phone step - never the search term, a name, a number, message
 * text or a token. Unknown values are dropped, never echoed back.
 */

export const STATUSES = ["open", "pending", "closed", "all"] as const;
export const MODES = ["ai", "human"] as const;
export const PRIORITIES = ["normal", "high", "urgent"] as const;

export type InboxStatus = (typeof STATUSES)[number];
export type InboxMode = (typeof MODES)[number];
export type InboxPriority = (typeof PRIORITIES)[number];

/** `me`, `none` or a member's id (an opaque UUID, never a name). */
export type AssignedTo = string;

export interface InboxView {
  status: InboxStatus | null;
  assignedTo: AssignedTo | null;
  mode: InboxMode | null;
  priority: InboxPriority | null;
  unread: boolean;
}

export const DEFAULT_VIEW: InboxView = Object.freeze({
  status: null,
  assignedTo: null,
  mode: null,
  priority: null,
  unread: false,
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

/** The view a URL's search params name; anything unknown is dropped. */
export function parseView(params: URLSearchParams): InboxView {
  const assigned = params.get("assigned_to");
  return {
    status: oneOf(params.get("status"), STATUSES),
    assignedTo:
      assigned === "me" || assigned === "none" || (assigned !== null && UUID.test(assigned))
        ? assigned.toLocaleLowerCase("en-US")
        : null,
    mode: oneOf(params.get("mode"), MODES),
    priority: oneOf(params.get("priority"), PRIORITIES),
    unread: params.get("unread") === "1",
  };
}

/** The search params of a view: only its own keys, in a fixed order. */
export function viewParams(view: InboxView): URLSearchParams {
  const params = new URLSearchParams();
  if (view.status) params.set("status", view.status);
  if (view.assignedTo) params.set("assigned_to", view.assignedTo);
  if (view.mode) params.set("mode", view.mode);
  if (view.priority) params.set("priority", view.priority);
  if (view.unread) params.set("unread", "1");
  return params;
}

export type PhoneStep = "list" | "conversation" | "panel";

/**
 * The address of a place in the inbox. `q` cannot be passed: the builder has no parameter for it,
 * so no caller can put a search term into the address bar (IL-05).
 */
export function inboxHref(
  view: InboxView,
  conversationId?: string | null,
  options?: { panel?: boolean },
): string {
  const params = viewParams(view);
  if (conversationId && options?.panel) params.set("panel", "customer");
  const path = conversationId && UUID.test(conversationId) ? `/inbox/${conversationId}` : "/inbox";
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export function sameView(a: InboxView, b: InboxView): boolean {
  return (
    a.status === b.status &&
    a.assignedTo === b.assignedTo &&
    a.mode === b.mode &&
    a.priority === b.priority &&
    a.unread === b.unread
  );
}

/** The query the API is asked: the view, plus the search term held only in memory. */
export interface ListQuery {
  status?: InboxStatus;
  assigned_to?: string;
  mode?: InboxMode;
  priority?: InboxPriority;
  unread?: boolean;
  q?: string;
  cursor?: string;
  limit?: number;
}

/**
 * The request a page of the list makes (IL-04). It has no `updated_since` parameter at all, so it
 * can never combine catching up with a view (BE1's 422 inbox_view_with_updated_since).
 */
export function listQuery(
  view: InboxView,
  search: string | null,
  cursor: string | null,
  limit: number,
): ListQuery {
  const query: ListQuery = { limit };
  if (view.status) query.status = view.status;
  if (view.assignedTo) query.assigned_to = view.assignedTo;
  if (view.mode) query.mode = view.mode;
  if (view.priority) query.priority = view.priority;
  if (view.unread) query.unread = true;
  if (search) query.q = search;
  if (cursor) query.cursor = cursor;
  return query;
}

/** The key part of a view (the search term is added in memory only - the key is never persisted). */
export function viewKey(view: InboxView): Record<string, string | boolean | null> {
  return {
    status: view.status,
    assigned_to: view.assignedTo,
    mode: view.mode,
    priority: view.priority,
    unread: view.unread,
  };
}

/** The search term's bounds after trimming (BE1: 2..100, else 422 search_term_invalid). */
export const SEARCH_MIN = 2;
export const SEARCH_MAX = 100;
export const SEARCH_DEBOUNCE_MS = 300;

export type SearchCheck =
  { kind: "none" } | { kind: "ok"; term: string } | { kind: "too-short" } | { kind: "too-long" };

export function checkSearch(raw: string): SearchCheck {
  const term = raw.trim();
  if (!term) return { kind: "none" };
  if (term.length < SEARCH_MIN) return { kind: "too-short" };
  if (term.length > SEARCH_MAX) return { kind: "too-long" };
  return { kind: "ok", term };
}

export function isConversationId(value: string | null | undefined): value is string {
  return typeof value === "string" && UUID.test(value);
}
