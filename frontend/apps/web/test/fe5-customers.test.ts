/**
 * FE-5's pure rules (ADR-199): what may live in a URL, what a search sends, what a lead's PATCH
 * carries, what a budget is, and what a status move and an assignment always name. Each is the
 * unit test a mutant of section 23 must meet (M-T25, M-T26, M-T28 .. M-T31, M-T36, M-T37, M-T42).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  addTag,
  assignmentBody,
  checkBudget,
  checkCurrency,
  CONTACT_NAME_MAX,
  formatBudget,
  formOf,
  LEAD_BOUNDS,
  leadPatch,
  leadProblems,
  statusMoveBody,
  toLatinDigits,
} from "../lib/customers/lead-form";
import {
  checkTerm,
  CONTACT_SEARCH_MAX,
  contactHref,
  contactsHref,
  DEFAULT_LEADS_VIEW,
  LEAD_SEARCH_MAX,
  LEAD_STATUSES,
  LEAD_TAG_MAX,
  leadHref,
  leadsHref,
  leadsQuery,
  parseContactsView,
  parseLeadsView,
} from "../lib/customers/view";
import { currentEntry, NAVIGATION } from "../lib/navigation";

const ID = "3f2b8c1e-5a47-4d0a-9f1e-2b6c7d8e9f01";
const OTHER = "9a8b7c6d-1e2f-4a3b-8c4d-5e6f7a8b9c0d";

describe("the URL of the customers screens (LY5-02)", () => {
  it("contacts: only the connected number; a search term has no parameter (M-T25)", () => {
    expect(contactsHref({ accountId: null })).toBe("/contacts");
    expect(contactsHref({ accountId: ID })).toBe(`/contacts?account_id=${ID}`);
    const href = contactsHref({ accountId: ID, q: "ipek" } as never);
    expect(new URL(href, "http://x").searchParams.has("q")).toBe(false);
    expect([...new URL(href, "http://x").searchParams.keys()]).toEqual(["account_id"]);
  });

  it("contacts: an unknown or malformed value is dropped, never echoed", () => {
    expect(parseContactsView(new URLSearchParams("account_id=<script>&q=ipek"))).toEqual({
      accountId: null,
    });
    expect(
      parseContactsView(new URLSearchParams(`account_id=${ID.toLocaleUpperCase("en")}`)),
    ).toEqual({
      accountId: ID,
    });
    expect(contactsHref({ accountId: "not-an-id" })).toBe("/contacts");
  });

  it("leads: status, source and assignee only; search and tag have no parameter (M-T26)", () => {
    expect(leadsHref(DEFAULT_LEADS_VIEW)).toBe("/leads");
    const href = leadsHref({
      status: "qualified",
      source: "manual",
      assignee: "me",
      search: "istanbul",
      tag: "vip",
    } as never);
    expect([...new URL(href, "http://x").searchParams.keys()].sort()).toEqual([
      "assignee",
      "source",
      "status",
    ]);
    expect(href).not.toContain("istanbul");
    expect(href).not.toContain("vip");
  });

  it("leads: the parser keeps closed vocabularies and ids, and drops the rest", () => {
    expect(
      parseLeadsView(new URLSearchParams("status=won&source=agent&assignee=none&search=x&tag=y")),
    ).toEqual({ status: "won", source: "agent", assignee: "none" });
    expect(parseLeadsView(new URLSearchParams(`assignee=${OTHER}`)).assignee).toBe(OTHER);
    expect(parseLeadsView(new URLSearchParams("status=archived&source=fax&assignee=Nour"))).toEqual(
      DEFAULT_LEADS_VIEW,
    );
  });

  it("a record's link takes an id and nothing else", () => {
    expect(contactHref(ID)).toBe(`/contacts/${ID}`);
    expect(leadHref(ID)).toBe(`/leads/${ID}`);
    expect(contactHref("../admin")).toBe("/contacts");
    expect(leadHref("Nour Test")).toBe("/leads");
  });
});

describe("what a search sends (TF-03, M-T36)", () => {
  it.each(["İpek", "IŞIK", "ıstanbul", "ISIK", "Ğül Çınar"])(
    "%s is sent exactly as typed - never folded or re-cased by the browser",
    (typed) => {
      expect(checkTerm(`  ${typed}  `, CONTACT_SEARCH_MAX)).toEqual({ kind: "ok", term: typed });
    },
  );

  it("bounds: empty is no search, too long is never sent", () => {
    expect(checkTerm("   ", CONTACT_SEARCH_MAX)).toEqual({ kind: "none" });
    expect(checkTerm("x".repeat(CONTACT_SEARCH_MAX + 1), CONTACT_SEARCH_MAX)).toEqual({
      kind: "too-long",
    });
    expect(checkTerm("x".repeat(LEAD_SEARCH_MAX), LEAD_SEARCH_MAX).kind).toBe("ok");
    expect(checkTerm("x".repeat(LEAD_TAG_MAX + 1), LEAD_TAG_MAX).kind).toBe("too-long");
  });

  it("the leads request carries the view, and the text only in its query", () => {
    expect(
      leadsQuery(
        { status: "new", source: null, assignee: "me" },
        { viewerId: ID, search: "İpek", tag: "vip", cursor: null, limit: 30 },
      ),
    ).toEqual({ limit: 30, status: ["new"], assigned_to_id: ID, tag: ["vip"], search: "İpek" });
    expect(
      leadsQuery(
        { status: null, source: "agent", assignee: "none" },
        { viewerId: ID, search: null, tag: null, cursor: "c1", limit: 30 },
      ),
    ).toEqual({ limit: 30, source: ["agent"], unassigned: true, cursor: "c1" });
  });
});

describe("a lead's PATCH (LD-02)", () => {
  const stored = formOf({
    name: "Nour Test",
    phone: "+20 10 5550 0001",
    email: "nour@example.test",
    interest: "Apartment finishing",
    budget_amount: "500000.00",
    budget_currency: "EGP",
    tags: ["vip"],
  });
  const DETAILS = ["name", "phone", "email"] as const;

  it("one field changed → one key (M-T30)", () => {
    expect(leadPatch(stored, { ...stored, email: "n.test@example.test" }, DETAILS)).toEqual({
      email: "n.test@example.test",
    });
  });

  it("a field the person emptied → null, not omitted (M-T31)", () => {
    expect(leadPatch(stored, { ...stored, phone: "   " }, DETAILS)).toEqual({ phone: null });
  });

  it("nothing changed → no request at all", () => {
    expect(leadPatch(stored, { ...stored }, DETAILS)).toBeNull();
    expect(leadPatch(stored, { ...stored, name: " Nour Test " }, DETAILS)).toBeNull();
  });

  it("saving one section never sends another section's edit", () => {
    const edited = { ...stored, email: "x@example.test", interest: "Villa" };
    expect(leadPatch(stored, edited, DETAILS)).toEqual({ email: "x@example.test" });
    expect(leadPatch(stored, edited, ["interest", "budget_amount", "budget_currency"])).toEqual({
      interest: "Villa",
    });
  });

  it("the same amount written another way is not a change; tags are sent whole", () => {
    expect(leadPatch(stored, { ...stored, budget_amount: "500000" }, ["budget_amount"])).toBeNull();
    expect(leadPatch(stored, { ...stored, budget_amount: "" }, ["budget_amount"])).toEqual({
      budget_amount: null,
    });
    expect(leadPatch(stored, { ...stored, tags: ["vip", "hot"] }, ["tags"])).toEqual({
      tags: ["vip", "hot"],
    });
    expect(leadPatch(stored, { ...stored, tags: ["vip"] }, ["tags"])).toBeNull();
  });
});

describe("a budget is a plain number (LL-04, M-T42)", () => {
  it.each(["500k", "1,000", "12e3", "-5", "EGP 500", "5 00", "1.234"])(
    "%s is refused before any request",
    (typed) => {
      expect(checkBudget(typed)).toEqual({ ok: false });
      expect(leadProblems({ ...formOf({}), budget_amount: typed }, ["budget_amount"])).toEqual({
        budget_amount: "budget",
      });
    },
  );

  it("digits pass; Arabic-Indic digits are sent as Latin ones", () => {
    expect(checkBudget("500000")).toEqual({ ok: true, value: "500000" });
    expect(checkBudget("٥٠٠٠٠٠")).toEqual({ ok: true, value: "500000" });
    expect(checkBudget("۱۲٫۵")).toEqual({ ok: true, value: "12.5" });
    expect(toLatinDigits("٠١٢٣٤٥٦٧٨٩")).toBe("0123456789");
    expect(checkBudget("")).toEqual({ ok: true, value: null });
  });

  it("a currency is three letters; money is drawn with the API's currency", () => {
    expect(checkCurrency("egp")).toEqual({ ok: true, value: "EGP" });
    expect(checkCurrency("try")).toEqual({ ok: true, value: "TRY" });
    expect(checkCurrency("pounds")).toEqual({ ok: false });
    expect(formatBudget("500000.00", "EGP", "en")).toContain("500,000");
    expect(formatBudget(null, "EGP", "en")).toBeNull();
    expect(formatBudget("12", null, "en")).toBe("12");
  });

  it("tags: trimmed, bounded, never doubled", () => {
    expect(addTag(["vip"], "  hot ")).toEqual(["vip", "hot"]);
    expect(addTag(["vip"], "vip")).toEqual(["vip"]);
    expect(addTag([], "x".repeat(LEAD_BOUNDS.tag + 1))).toBeNull();
    expect(addTag([], "a\u0000b")).toBeNull();
  });
});

describe("a move and an assignment always name what the page showed (LD-03, LD-05)", () => {
  it("a status move carries expected_status (M-T28)", () => {
    expect(statusMoveBody({ status: "new" }, "contacted")).toEqual({
      status: "contacted",
      expected_status: "new",
    });
  });

  it("an assignment carries expected_assigned_to_id, and null is a value (M-T29)", () => {
    expect(assignmentBody({ assigned_to_id: null }, ID)).toEqual({
      assigned_to_id: ID,
      expected_assigned_to_id: null,
    });
    expect(assignmentBody({ assigned_to_id: ID }, null)).toEqual({
      assigned_to_id: null,
      expected_assigned_to_id: ID,
    });
  });
});

describe("the bounds restate the contract (openapi.json)", () => {
  const contract = JSON.parse(
    readFileSync(resolve(__dirname, "../../../packages/api-client/openapi.json"), "utf8"),
  ) as { components: { schemas: Record<string, { properties: Record<string, unknown> }> } };
  const max = (schema: string, field: string): number | undefined => {
    const property = contract.components.schemas[schema]!.properties[field] as {
      maxLength?: number;
      maximum?: number;
      anyOf?: Array<{ maxLength?: number }>;
    };
    return (
      property.maxLength ??
      property.maximum ??
      property.anyOf?.find((item) => item.maxLength !== undefined)?.maxLength
    );
  };

  it.each([
    ["LeadCreateRequest", "name", LEAD_BOUNDS.name],
    ["LeadCreateRequest", "phone", LEAD_BOUNDS.phone],
    ["LeadCreateRequest", "email", LEAD_BOUNDS.email],
    ["LeadCreateRequest", "interest", LEAD_BOUNDS.interest],
    ["LeadUpdateRequest", "budget_currency", LEAD_BOUNDS.currency],
    ["LeadNoteRequest", "body", LEAD_BOUNDS.note],
    ["LeadScoreRequest", "score", LEAD_BOUNDS.scoreMax],
    ["ContactUpdateRequest", "name", CONTACT_NAME_MAX],
  ] as const)("%s.%s is at most %i", (schema, field, bound) => {
    expect(max(schema, field)).toBe(bound);
  });

  it("the statuses are the contract's", () => {
    const statuses = (contract.components.schemas["LeadStatus"] as unknown as { enum: string[] })
      .enum;
    expect([...LEAD_STATUSES]).toEqual(statuses);
  });
});

describe("the navigation (CL-01)", () => {
  it("Customers and Leads are entries of every role, and a record's page keeps its entry current", () => {
    const keys = NAVIGATION.map((entry) => entry.key);
    expect(keys).toEqual(["home", "inbox", "contacts", "leads", "channels"]);
    expect(currentEntry(`/contacts/${ID}`)).toBe("contacts");
    expect(currentEntry(`/leads/${ID}`)).toBe("leads");
  });
});

describe("one ICU message per sentence (FEF-33, M-T37)", () => {
  const pane = readFileSync(
    resolve(__dirname, "../components/inbox/conversation-pane.tsx"),
    "utf8",
  );

  it('"Assigned to <name>" is one rich message, never a fragment beside a value', () => {
    expect(pane).toContain('t.rich("assignedTo"');
    expect(pane).not.toMatch(/\{t\("assignedTo"\)\}/);
  });

  it.each(["en", "ar", "fr", "tr"])("%s: the sentence holds its own name placeholder", (locale) => {
    const messages = JSON.parse(
      readFileSync(
        resolve(__dirname, `../../../packages/i18n/messages/${locale}/app.json`),
        "utf8",
      ),
    ) as { inbox: { assignedTo: string; seenBy: string } };
    expect(messages.inbox.assignedTo).toContain("<name></name>");
    expect(messages.inbox.seenBy).toContain("<name></name>");
  });
});
