/**
 * The contacts and leads lists' views and their place in the URL (LY5-02, CL-02, LL-01). A view
 * maps one to one to the API's parameters; nothing is invented. The URL carries only ids and closed
 * vocabularies - the connected number, a status, a source, an assignee - and never a search term,
 * a tag, a name, a number or any text a person typed: the builders have no parameter for them, so
 * no caller can put one into the address bar. Unknown values are dropped, never echoed back.
 */

export const LEAD_STATUSES = ["new", "contacted", "qualified", "proposal", "won", "lost"] as const;
export const LEAD_SOURCES = ["whatsapp", "agent", "manual", "import"] as const;
/** The statuses in which a lead is still open (the API's one-open-lead rule). */
export const OPEN_LEAD_STATUSES = ["new", "contacted", "qualified", "proposal"] as const;

export type LeadStatus = (typeof LEAD_STATUSES)[number];
export type LeadSource = (typeof LEAD_SOURCES)[number];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isId(value: string | null | undefined): value is string {
  return typeof value === "string" && UUID.test(value);
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

/* ------------------------------------------------------------------ contacts */

export interface ContactsView {
  /** The connected number (an opaque id), or null for every contact. */
  accountId: string | null;
}

export function parseContactsView(params: URLSearchParams): ContactsView {
  const account = params.get("account_id");
  return { accountId: isId(account) ? account.toLocaleLowerCase("en") : null };
}

export function contactsHref(view: ContactsView): string {
  const params = new URLSearchParams();
  if (view.accountId && isId(view.accountId)) params.set("account_id", view.accountId);
  const query = params.toString();
  return query ? `/contacts?${query}` : "/contacts";
}

export function contactHref(id: string): string {
  return isId(id) ? `/contacts/${id}` : "/contacts";
}

/* ------------------------------------------------------------------ leads */

/** `me`, `none` (unassigned) or a member's id - never a name. */
export type LeadAssignee = string;

export interface LeadsView {
  status: LeadStatus | null;
  source: LeadSource | null;
  assignee: LeadAssignee | null;
}

export const DEFAULT_LEADS_VIEW: LeadsView = Object.freeze({
  status: null,
  source: null,
  assignee: null,
});

export function parseLeadsView(params: URLSearchParams): LeadsView {
  const assignee = params.get("assignee");
  return {
    status: oneOf(params.get("status"), LEAD_STATUSES),
    source: oneOf(params.get("source"), LEAD_SOURCES),
    assignee:
      assignee === "me" || assignee === "none"
        ? assignee
        : isId(assignee)
          ? assignee.toLocaleLowerCase("en")
          : null,
  };
}

export function leadsHref(view: LeadsView): string {
  const params = new URLSearchParams();
  if (view.status) params.set("status", view.status);
  if (view.source) params.set("source", view.source);
  if (view.assignee) params.set("assignee", view.assignee);
  const query = params.toString();
  return query ? `/leads?${query}` : "/leads";
}

export function leadHref(id: string): string {
  return isId(id) ? `/leads/${id}` : "/leads";
}

export function leadsViewKey(view: LeadsView): Record<string, string | null> {
  return { status: view.status, source: view.source, assignee: view.assignee };
}

export function isDefaultLeadsView(view: LeadsView): boolean {
  return !view.status && !view.source && !view.assignee;
}

/** The request one page of the leads list makes: the view, and the text held only in memory. */
export interface LeadsQuery {
  status?: LeadStatus[];
  source?: LeadSource[];
  assigned_to_id?: string;
  unassigned?: boolean;
  tag?: string[];
  search?: string;
  cursor?: string;
  limit: number;
}

export function leadsQuery(
  view: LeadsView,
  options: {
    viewerId: string | null;
    search: string | null;
    tag: string | null;
    cursor: string | null;
    limit: number;
  },
): LeadsQuery {
  const query: LeadsQuery = { limit: options.limit };
  if (view.status) query.status = [view.status];
  if (view.source) query.source = [view.source];
  if (view.assignee === "none") query.unassigned = true;
  else if (view.assignee === "me") {
    if (options.viewerId) query.assigned_to_id = options.viewerId;
  } else if (view.assignee) query.assigned_to_id = view.assignee;
  if (options.tag) query.tag = [options.tag];
  if (options.search) query.search = options.search;
  if (options.cursor) query.cursor = options.cursor;
  return query;
}

/* ------------------------------------------------------------------ search text (memory only) */

export const SEARCH_DEBOUNCE_MS = 300;
/** GET /contacts `q`: at most 100 characters. */
export const CONTACT_SEARCH_MAX = 100;
/** GET /leads `search`: 1 to 200 characters; `tag`: at most 50. */
export const LEAD_SEARCH_MAX = 200;
export const LEAD_TAG_MAX = 50;

export type TermCheck = { kind: "none" } | { kind: "ok"; term: string } | { kind: "too-long" };

/**
 * The term as the API is asked it: trimmed and bounded, and otherwise exactly what the person
 * typed. It is never lower-cased, upper-cased or folded here (TF-03) - the server's one folding
 * rule decides what matches, in every locale.
 */
export function checkTerm(raw: string, max: number): TermCheck {
  const term = raw.trim();
  if (!term) return { kind: "none" };
  if (term.length > max) return { kind: "too-long" };
  return { kind: "ok", term };
}
