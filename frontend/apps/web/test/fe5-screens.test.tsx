// @vitest-environment jsdom
/**
 * FE-5's screens against a recorded API (ADR-199): what a role sees, which requests a screen makes
 * and what a move sends. The session and the workspace are doubles; the components, the query
 * client and the catalogue are the real ones (M-T27, M-T28, M-T32, M-T33, M-T41, M-T43).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";

import { ContactDetail } from "../components/customers/contact-detail";
import { LeadStatusControl } from "../components/customers/lead-status";
import { LeadsList } from "../components/customers/leads-list";
import type { LeadRead } from "../components/customers/shared";

const ME = "11111111-1111-4111-8111-111111111111";
const CONTACT = "22222222-2222-4222-8222-222222222222";
const LEAD = "33333333-3333-4333-8333-333333333333";

let role: "tenant_owner" | "tenant_admin" | "member" = "member";
type Call = { method: string; path: string; init: Record<string, unknown> | undefined };
let calls: Call[] = [];
let answers: Record<string, (init: Record<string, unknown> | undefined) => unknown> = {};
let pending: Array<() => void> = [];

function answer(method: string, path: string, init?: Record<string, unknown>) {
  calls.push({ method, path, init });
  const make = answers[`${method} ${path}`];
  const result = make ? make(init) : { __status: 404, error: { code: "not_found" } };
  const failed = (result as { __status?: number } | null)?.__status;
  return Promise.resolve(
    failed
      ? { error: result, response: new Response("{}", { status: failed }) }
      : { data: result, response: new Response("{}", { status: 200 }) },
  );
}

const api = {
  GET: (path: string, init?: Record<string, unknown>) => answer("GET", path, init),
  POST: (path: string, init?: Record<string, unknown>) => answer("POST", path, init),
  PATCH: (path: string, init?: Record<string, unknown>) => answer("PATCH", path, init),
  DELETE: (path: string, init?: Record<string, unknown>) => answer("DELETE", path, init),
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/leads",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({
    status: "signed-in",
    profile: { id: ME },
    profileLoaded: true,
    session: { api },
  }),
}));

vi.mock("../components/app/workspace-context", () => ({
  useWorkspace: () => ({
    workspaceId: "ws",
    scope: { workspaceId: "ws", sessionGeneration: 1 },
    role,
  }),
}));

const en = messagesFor("en")["app"] as {
  customers: {
    consent: Record<string, string>;
    create: Record<string, string>;
    status: { change: string; wonClosed: string; reopen: string; moveTo: Record<string, string> };
    stats: Record<string, string>;
  };
};

function lead(overrides: Partial<LeadRead> = {}): LeadRead {
  return {
    id: LEAD,
    contact_id: CONTACT,
    conversation_id: null,
    name: "Nour Test",
    phone: null,
    email: null,
    interest: null,
    budget_amount: null,
    budget_currency: null,
    status: "new",
    next_statuses: ["contacted", "lost"],
    source: "manual",
    score: 10,
    assigned_to_id: null,
    tags: [],
    custom_fields: {},
    human_verified_fields: [],
    qualified_at: null,
    closed_at: null,
    last_activity_at: null,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    ...overrides,
  };
}

const page = (items: unknown[]) => ({ items, next_cursor: null });

function contact(optedOut: boolean) {
  return {
    id: CONTACT,
    identities: [{ id: "i1", channel: "whatsapp", kind: "phone", value: "+201055500001" }],
    profile_name: "Nour Test",
    name: null,
    display_name: "Nour Test",
    channels: [
      {
        channel: "whatsapp",
        identities: [],
        marketing_opt_out_at: optedOut ? "2026-11-02T10:00:00Z" : null,
        opt_out_source: optedOut ? "team" : null,
      },
    ],
    opted_out: optedOut,
    opted_out_at: optedOut ? "2026-11-02T10:00:00Z" : null,
    created_at: "2026-11-01T10:00:00Z",
    last_inbound_at: null,
  };
}

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  role = "member";
  calls = [];
  pending = [];
  answers = {
    "GET /api/v1/workspace/members": () => ({ members: [] }),
    "GET /api/v1/leads": () => page([lead()]),
    "GET /api/v1/leads/statistics": () => ({
      total: 4,
      open_leads: 3,
      unassigned: 1,
      by_status: { won: 1 },
    }),
    "GET /api/v1/contacts/{contact_id}": () => contact(true),
    "GET /api/v1/contacts/{contact_id}/consent-events": () => page([]),
    "GET /api/v1/conversations": () => page([]),
  };
});

afterEach(() => cleanup());

const sent = (method: string, path: string) =>
  calls.filter((call) => call.method === method && call.path === path);

describe("the statistics are an administrator's (LL-03, M-T32)", () => {
  it("a member's leads page never sends the request and draws no strip", async () => {
    render(<LeadsList />, { wrapper: Wrapper });
    await screen.findByRole("list", { name: "Leads" });
    expect(sent("GET", "/api/v1/leads")).toHaveLength(1);
    expect(sent("GET", "/api/v1/leads/statistics")).toHaveLength(0);
    expect(screen.queryByRole("group", { name: en.customers.stats["label"]! })).toBeNull();
  });

  it.each(["tenant_owner", "tenant_admin"] as const)("%s: one request, the strip", async (as) => {
    role = as;
    render(<LeadsList />, { wrapper: Wrapper });
    await screen.findByRole("group", { name: en.customers.stats["label"]! });
    expect(sent("GET", "/api/v1/leads/statistics")).toHaveLength(1);
  });
});

describe("clearing an opt-out is an administrator's (CL-03, M-T33)", () => {
  it("a member sees the opted-out state and no clear control - not even a disabled one", async () => {
    render(<ContactDetail id={CONTACT} />, { wrapper: Wrapper });
    await screen.findByRole("heading", { name: en.customers.consent["title"]! });
    expect(screen.queryByRole("button", { name: en.customers.consent["clear"]! })).toBeNull();
    expect(document.querySelector('[data-consent-state="opted-out"]')).not.toBeNull();
  });

  it("an owner sees it; any member may record an opt-out", async () => {
    role = "tenant_owner";
    render(<ContactDetail id={CONTACT} />, { wrapper: Wrapper });
    expect(
      await screen.findByRole("button", { name: en.customers.consent["clear"]! }),
    ).toBeTruthy();
    cleanup();
    role = "member";
    answers["GET /api/v1/contacts/{contact_id}"] = () => contact(false);
    render(<ContactDetail id={CONTACT} />, { wrapper: Wrapper });
    expect(
      await screen.findByRole("button", { name: en.customers.consent["record"]! }),
    ).toBeTruthy();
  });
});

describe('"Create lead" on a customer (LD-10, M-T43)', () => {
  it("is absent while the customer has an open lead", async () => {
    answers["GET /api/v1/leads"] = () => page([lead({ status: "qualified" })]);
    render(<ContactDetail id={CONTACT} />, { wrapper: Wrapper });
    await screen.findByRole("list", { name: "Leads" });
    expect(screen.queryByRole("button", { name: en.customers.create["open"]! })).toBeNull();
  });

  it("is offered once every lead is closed", async () => {
    answers["GET /api/v1/leads"] = () =>
      page([lead({ status: "won", next_statuses: [] as LeadRead["next_statuses"] })]);
    render(<ContactDetail id={CONTACT} />, { wrapper: Wrapper });
    expect(await screen.findByRole("button", { name: en.customers.create["open"]! })).toBeTruthy();
  });
});

describe("a lead's status (LD-03)", () => {
  async function openMenu() {
    const trigger = screen.getByRole("button", { name: en.customers.status.change });
    // Radix opens its menu on a primary pointer-down; jsdom has no PointerEvent of its own.
    fireEvent(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0, ctrlKey: false }));
    return screen.findAllByRole("menuitem");
  }

  it("offers exactly next_statuses - never a status from a copy of the graph (M-T27)", async () => {
    render(<LeadStatusControl lead={lead()} />, { wrapper: Wrapper });
    const items = await openMenu();
    expect(items.map((item) => item.textContent)).toEqual([
      en.customers.status.moveTo["contacted"],
      en.customers.status.moveTo["lost"],
    ]);
  });

  it("a won lead has no menu, only its words; a lost one offers the reopen", async () => {
    render(
      <LeadStatusControl
        lead={lead({ status: "won", next_statuses: [] as LeadRead["next_statuses"] })}
      />,
      { wrapper: Wrapper },
    );
    expect(screen.queryByRole("button", { name: en.customers.status.change })).toBeNull();
    expect(screen.getByText(en.customers.status.wonClosed)).toBeTruthy();
    cleanup();
    render(
      <LeadStatusControl
        lead={lead({ status: "lost", next_statuses: ["new"] as LeadRead["next_statuses"] })}
      />,
      { wrapper: Wrapper },
    );
    const items = await openMenu();
    expect(items.map((item) => item.textContent)).toEqual([en.customers.status.reopen]);
  });

  it("a move sends expected_status once, and the badge waits for the answer (M-T28, M-T41)", async () => {
    answers["POST /api/v1/leads/{lead_id}/status"] = () => {
      // Never answers during this test: the screen must still show what is stored.
      return new Promise<never>((resolve) => pending.push(resolve as () => void));
    };
    render(<LeadStatusControl lead={lead()} />, { wrapper: Wrapper });
    const [contacted] = await openMenu();
    fireEvent.click(contacted!);
    await waitFor(() => expect(sent("POST", "/api/v1/leads/{lead_id}/status")).toHaveLength(1));
    const [move] = sent("POST", "/api/v1/leads/{lead_id}/status");
    expect(move!.init?.["body"]).toEqual({ status: "contacted", expected_status: "new" });
    expect(document.querySelector("[data-lead-status]")?.getAttribute("data-lead-status")).toBe(
      "new",
    );
  });

  it("a stale move reloads the lead, says where it stands and re-sends nothing", async () => {
    answers["POST /api/v1/leads/{lead_id}/status"] = () => ({
      __status: 409,
      error: { code: "stale_lead_status", message: "x" },
    });
    answers["GET /api/v1/leads/{lead_id}"] = () =>
      lead({
        status: "qualified",
        next_statuses: ["proposal", "lost"] as LeadRead["next_statuses"],
      });
    render(<LeadStatusControl lead={lead()} />, { wrapper: Wrapper });
    const [contacted] = await openMenu();
    fireEvent.click(contacted!);
    await waitFor(() =>
      expect(document.querySelector('[data-move-notice="stale"]')?.textContent).toContain(
        "Qualified",
      ),
    );
    expect(sent("POST", "/api/v1/leads/{lead_id}/status")).toHaveLength(1);
    expect(sent("GET", "/api/v1/leads/{lead_id}")).toHaveLength(1);
  });
});
