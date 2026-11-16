/**
 * What the lead screens send (LD-02, LL-04, docs/CRM.md): a PATCH carries only the fields the
 * person changed - an untouched field is omitted, an emptied one is `null` - and a budget is a
 * plain number or it is not sent at all. Pure, so every rule is unit-tested.
 *
 * The bounds below restate the contract's (LeadCreateRequest / LeadUpdateRequest in
 * openapi.json); `test/customers-bounds.test.ts` reads the contract and fails if one drifts.
 */

export const LEAD_BOUNDS = {
  name: 200,
  phone: 32,
  email: 320,
  interest: 500,
  currency: 3,
  note: 5000,
  tag: 50,
  tags: 25,
  scoreMin: 0,
  scoreMax: 100,
  /** `Numeric(14, 2)`: at most twelve digits before the point and two after. */
  budgetIntegerDigits: 12,
  budgetDecimals: 2,
} as const;

/** The member-given name of a contact (ContactUpdateRequest.name). */
export const CONTACT_NAME_MAX = 200;

export const TEXT_FIELDS = ["name", "phone", "email", "interest"] as const;
export type TextField = (typeof TEXT_FIELDS)[number];

export interface LeadFormValues {
  name: string;
  phone: string;
  email: string;
  interest: string;
  budget_amount: string;
  budget_currency: string;
  tags: string[];
}

export const EMPTY_LEAD_FORM: LeadFormValues = Object.freeze({
  name: "",
  phone: "",
  email: "",
  interest: "",
  budget_amount: "",
  budget_currency: "",
  tags: [],
});

const DIGIT_RANGES: Array<[number, number]> = [
  [0x0660, 0x0669], // Arabic-Indic
  [0x06f0, 0x06f9], // Eastern Arabic-Indic
];

/** Arabic-Indic digits a person typed, as the Latin digits the API reads; the rest unchanged. */
export function toLatinDigits(text: string): string {
  let out = "";
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    const range = DIGIT_RANGES.find(([from, to]) => code >= from && code <= to);
    out += range ? String(code - range[0]) : char;
  }
  // The Arabic decimal separator, typed with those digits.
  return out.replace(/٫/g, ".");
}

export type BudgetCheck = { ok: true; value: string | null } | { ok: false };

/**
 * A budget as typed → the number the API takes, or a refusal. Empty is "no budget" (null).
 * Anything that is not plain digits with at most two decimals is refused here, before any
 * request: "500k", "1,000", "12e3", a currency sign, a negative.
 */
export function checkBudget(raw: string): BudgetCheck {
  const text = toLatinDigits(raw).trim();
  if (!text) return { ok: true, value: null };
  const pattern = new RegExp(
    `^\\d{1,${LEAD_BOUNDS.budgetIntegerDigits}}(\\.\\d{1,${LEAD_BOUNDS.budgetDecimals}})?$`,
  );
  if (!pattern.test(text)) return { ok: false };
  return { ok: true, value: text };
}

/** A currency as typed → three letters in upper case (codes are ASCII: no locale casing), or a refusal. */
export function checkCurrency(raw: string): { ok: true; value: string | null } | { ok: false } {
  const text = raw.trim();
  if (!text) return { ok: true, value: null };
  if (!/^[A-Za-z]{3}$/.test(text)) return { ok: false };
  return { ok: true, value: text.toLocaleUpperCase("en") };
}

/** What the API's own patterns accept (lead_service.py), so a refusal shows before a request. */
export function looksLikeEmail(text: string): boolean {
  return /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(text);
}

export function looksLikePhone(text: string): boolean {
  return /^\+?[\d\s().-]{6,31}$/.test(text);
}

/** Text PostgreSQL cannot hold (CRM-12): never sent. */
export function hasNul(text: string): boolean {
  return text.includes("\u0000");
}

export type LeadPatch = Partial<{
  name: string | null;
  phone: string | null;
  email: string | null;
  interest: string | null;
  budget_amount: string | null;
  budget_currency: string | null;
  tags: string[];
}>;

function sameTags(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((tag, index) => tag === b[index]);
}

/**
 * The PATCH body for a form: only what differs from what the page read. An untouched field is
 * absent; a field the person emptied is `null`; tags are sent whole when they changed. Returns
 * null when nothing changed - then no request is made at all.
 *
 * `fields` names the section being saved, so saving one section never sends another's edits.
 */
export function leadPatch(
  initial: LeadFormValues,
  current: LeadFormValues,
  fields: ReadonlyArray<keyof LeadFormValues>,
): LeadPatch | null {
  const patch: LeadPatch = {};
  for (const field of fields) {
    if (field === "tags") {
      if (!sameTags(initial.tags, current.tags)) patch.tags = [...current.tags];
      continue;
    }
    const before = initial[field].trim();
    let after = current[field].trim();
    if (field === "budget_amount") {
      const budget = checkBudget(after);
      const was = checkBudget(before);
      if (!budget.ok) continue;
      // "500" and "500.00" are one amount: not a change.
      if (was.ok && Number(budget.value ?? "NaN") === Number(was.value ?? "NaN")) continue;
      if (was.ok && budget.value === null && was.value === null) continue;
      patch.budget_amount = budget.value;
      continue;
    }
    if (field === "budget_currency") after = after.toLocaleUpperCase("en");
    if (after === (field === "budget_currency" ? before.toLocaleUpperCase("en") : before)) continue;
    patch[field] = after === "" ? null : after;
  }
  return Object.keys(patch).length ? patch : null;
}

/** A tag as the API stores it (trimmed, lower-cased by the server's own rule - sent as typed). */
export function addTag(tags: readonly string[], raw: string): string[] | null {
  const tag = raw.trim();
  if (!tag || tag.length > LEAD_BOUNDS.tag || hasNul(tag)) return null;
  if (tags.length >= LEAD_BOUNDS.tags) return null;
  if (tags.includes(tag)) return [...tags];
  return [...tags, tag];
}

export type FieldProblem = "tooLong" | "email" | "phone" | "budget" | "currency" | "invalid";

/** The problems a form would be refused for, by field - shown under the field, nothing sent. */
export function leadProblems(
  values: LeadFormValues,
  fields: ReadonlyArray<keyof LeadFormValues>,
): Partial<Record<keyof LeadFormValues, FieldProblem>> {
  const problems: Partial<Record<keyof LeadFormValues, FieldProblem>> = {};
  for (const field of fields) {
    if (field === "tags") continue;
    const text = values[field].trim();
    if (!text) continue;
    if (hasNul(text)) problems[field] = "invalid";
    else if (field === "budget_amount") {
      if (!checkBudget(text).ok) problems[field] = "budget";
    } else if (field === "budget_currency") {
      if (!checkCurrency(text).ok) problems[field] = "currency";
    } else if (text.length > LEAD_BOUNDS[field]) problems[field] = "tooLong";
    else if (field === "email" && !looksLikeEmail(text)) problems[field] = "email";
    else if (field === "phone" && !looksLikePhone(text)) problems[field] = "phone";
  }
  return problems;
}

/** What a lead holds, as a form starts from it. */
export function formOf(lead: {
  name?: string | null;
  phone?: string | null;
  email?: string | null;
  interest?: string | null;
  budget_amount?: string | number | null;
  budget_currency?: string | null;
  tags?: readonly string[] | null;
}): LeadFormValues {
  return {
    name: lead.name ?? "",
    phone: lead.phone ?? "",
    email: lead.email ?? "",
    interest: lead.interest ?? "",
    budget_amount:
      lead.budget_amount === null || lead.budget_amount === undefined
        ? ""
        : String(lead.budget_amount),
    budget_currency: lead.budget_currency ?? "",
    tags: [...(lead.tags ?? [])],
  };
}

/** Money with the API's amount and currency, never computed here (plan section 20). */
export function formatBudget(
  amount: string | number | null | undefined,
  currency: string | null | undefined,
  locale: string,
): string | null {
  if (amount === null || amount === undefined || amount === "") return null;
  const value = Number(amount);
  if (!Number.isFinite(value)) return null;
  if (currency && /^[A-Za-z]{3}$/.test(currency)) {
    try {
      return new Intl.NumberFormat(`${locale}-u-nu-latn`, {
        style: "currency",
        currency: currency.toLocaleUpperCase("en"),
      }).format(value);
    } catch {
      // An unknown code: the number alone, with the code as the API gave it.
    }
  }
  const number = new Intl.NumberFormat(`${locale}-u-nu-latn`, { maximumFractionDigits: 2 }).format(
    value,
  );
  return currency ? `${number} ${currency}` : number;
}

/** The body of a status move: always with the status the screen was showing (LD-03). */
export function statusMoveBody<S extends string>(lead: { status: S }, to: S) {
  return { status: to, expected_status: lead.status };
}

/** The body of an assignment: always with the owner the page was showing; null is a value (LD-05). */
export function assignmentBody(lead: { assigned_to_id?: string | null }, to: string | null) {
  return { assigned_to_id: to, expected_assigned_to_id: lead.assigned_to_id ?? null };
}
