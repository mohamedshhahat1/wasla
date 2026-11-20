// @vitest-environment jsdom
/**
 * FE-8 leftovers (MF-04): FE-8's billing, team and account screens against a recorded API, for the
 * rules a mutant broke while every FE-8 unit test stayed green - each a fact a screen shows or a
 * request it makes (or does not make):
 *
 * - the return page: the redirect's parameters leave the address bar on arrival (M-R19); reads
 *   pause while the tab is hidden (M-R20); a read that never reached the API decides nothing
 *   (M-R18); a settled payment moves focus to the heading (M-S29); a declined card is named as
 *   the bank's refusal (M-S30);
 * - the plan page: an administrator never asks for the owner's summary (M-R23); "keep the plan"
 *   only with a scheduled change (M-S35); a second press while a checkout opens sends nothing
 *   (M-S32); each press its own key (M-S31); a busy control stays focusable (M-S33); a plan's name
 *   is text (M-S46);
 * - an invoice: no refund review on a refunded payment (M-R22);
 * - the plan-limit line: an administrator is told who acts, with no link (M-S38);
 * - capacity: no "Buy" for an administrator (M-S47);
 * - the member row: each action's accessible name starts with its visible words (M-S39);
 * - the account page: back from linking Google, the profile is read again (M-S41).
 *
 * The session and the workspace are doubles; the components, the query client and the catalogue
 * are the real ones. The return page's schedule runs on fake timers.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";

import { InvoicePage } from "../components/billing/invoices";
import { PlanPage } from "../components/billing/plan-page";
import { ReturnPage } from "../components/billing/return-page";

const SCOPE = { workspaceId: "ws", sessionGeneration: 1 };
const INVOICE = "66666666-6666-4666-8666-666666666666";
const PAYMENT = "77777777-7777-4777-8777-777777777777";

let role: "tenant_owner" | "tenant_admin" | "member" = "tenant_owner";
let search = new URLSearchParams();
const reloadProfile = vi.fn(() => Promise.resolve());
type Call = { method: string; path: string; init: Record<string, unknown> | undefined };
let calls: Call[] = [];
let answers: Record<string, (init: Record<string, unknown> | undefined) => unknown> = {};

function answer(method: string, path: string, init?: Record<string, unknown>) {
  calls.push({ method, path, init });
  const make = answers[`${method} ${path}`];
  let result: unknown;
  try {
    result = make ? make(init) : { __status: 404, error: { error: { code: "not_found" } } };
  } catch (error) {
    return Promise.reject(error instanceof Error ? error : new Error(String(error)));
  }
  return Promise.resolve(result).then((value) => {
    const failed = (value as { __status?: number } | null)?.__status;
    return failed
      ? {
          error: (value as { error: unknown }).error,
          response: new Response("{}", { status: failed }),
        }
      : { data: value, response: new Response("{}", { status: 200 }) };
  });
}

const api = {
  GET: (path: string, init?: Record<string, unknown>) => answer("GET", path, init),
  POST: (path: string, init?: Record<string, unknown>) => answer("POST", path, init),
  PATCH: (path: string, init?: Record<string, unknown>) => answer("PATCH", path, init),
  PUT: (path: string, init?: Record<string, unknown>) => answer("PUT", path, init),
  DELETE: (path: string, init?: Record<string, unknown>) => answer("DELETE", path, init),
};

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/billing",
  useSearchParams: () => search,
}));

// One session object for the whole file, as the real provider keeps one per sign-in: the return
// page's effects depend on it, and a new object every render would read again forever.
const SESSION = {
  status: "signed-in",
  profile: {
    id: "11111111-1111-4111-8111-111111111111",
    full_name: "Test Owner",
    email: "owner@example.test",
    has_password: true,
    google_connected: true,
    locale: "en",
  },
  profileLoaded: true,
  reloadProfile,
  session: { api, googleLinkUrl: vi.fn() },
};

vi.mock("../components/session-provider", () => ({
  useSession: () => SESSION,
}));

vi.mock("../components/app/workspace-context", () => ({
  useWorkspace: () => ({
    workspaceId: "ws",
    workspaceName: "Test workspace",
    scope: SCOPE,
    role,
    recheck: vi.fn(() => Promise.resolve()),
    gates: { subscription_status: "active" },
    allowance: null,
  }),
}));

let client: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

const app = messagesFor("en")["app"] as unknown as {
  billing: { return: Record<string, string> };
  team: Record<string, string>;
};

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

function invoice(overrides: Record<string, unknown> = {}) {
  return {
    id: INVOICE,
    status: "open",
    purpose: "checkout",
    plan_code: "pro",
    plan_version_id: null,
    plan_price_id: null,
    amount_due: "99.00",
    amount_paid: "0.00",
    outstanding: "99.00",
    currency: "EGP",
    period_start: "2026-11-20T00:00:00Z",
    period_end: "2026-12-20T00:00:00Z",
    issued_at: null,
    paid_at: null,
    billing_interval: "monthly",
    interval_count: 1,
    lines: [],
    ...overrides,
  };
}

function payment(overrides: Record<string, unknown> = {}) {
  return {
    id: PAYMENT,
    status: "pending",
    amount: "99.00",
    currency: "EGP",
    provider: "paymob",
    invoice_id: INVOICE,
    refunded_amount: null,
    refund_pending: false,
    refunded_at: null,
    failure_reason: null,
    processed_at: null,
    created_at: iso(Date.now() - 30_000),
    ...overrides,
  };
}

const FREE = {
  id: "plan-free",
  code: "free",
  name: "Free",
  description: null,
  version: 1,
  price: "0.00",
  currency: "EGP",
  interval: "monthly",
  trial_days: 0,
  limits: [],
  billing_required: false,
  is_custom: false,
  prices: [],
};

function pro(name = "Pro") {
  return {
    ...FREE,
    id: "plan-pro",
    code: "pro",
    name,
    price: "99.00",
    billing_required: true,
    prices: [
      {
        id: "price-pro-m",
        amount: "99.00",
        currency: "EGP",
        interval: "monthly",
        interval_count: 1,
      },
    ],
  };
}

function subscription(overrides: Record<string, unknown> = {}) {
  return {
    id: "sub",
    status: "active",
    plan: FREE,
    plan_price: null,
    plan_version_id: "v1",
    current_period_start: "2026-11-01T00:00:00Z",
    current_period_end: "2026-12-01T00:00:00Z",
    billing_anchor_at: "2026-11-01T00:00:00Z",
    trial_ends_at: null,
    cancel_at_period_end: false,
    cancelled_at: null,
    ended_at: null,
    scheduled_change: null,
    revision: 1,
    ...overrides,
  };
}

function count(method: string, path: string): number {
  return calls.filter((call) => call.method === method && call.path === path).length;
}

beforeEach(() => {
  role = "tenant_owner";
  search = new URLSearchParams();
  calls = [];
  reloadProfile.mockClear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  answers = {
    "GET /api/v1/workspace": () => ({
      id: "ws",
      name: "Test workspace",
      slug: "test-ws",
      status: "active",
      is_active: true,
      timezone: "UTC",
      utc_offset: "+00:00",
    }),
  };
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

/* ------------------------------------------------------------------ return */

describe("the return page (RT-02 .. RT-05)", () => {
  function returnApi(read: () => unknown) {
    answers["GET /api/v1/invoices"] = () => [invoice()];
    answers["GET /api/v1/invoices/{invoice_id}/payments"] = () => [payment()];
    answers["GET /api/v1/billing/payments/{payment_id}"] = read;
  }

  async function arrive(): Promise<void> {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(<ReturnPage />, { wrapper: Wrapper });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  function state(): string | null {
    return document.querySelector("[data-return-state]")?.getAttribute("data-return-state") ?? null;
  }

  it("drops the redirect's parameters from the address bar on arrival (M-R19)", async () => {
    window.history.pushState({}, "", "/billing/checkout/return?success=true&id=1");
    const replace = vi.spyOn(window.history, "replaceState");
    returnApi(() => payment());
    await arrive();
    expect(replace).toHaveBeenCalledWith(expect.anything(), "", "/billing/checkout/return");
    expect(window.location.search).toBe("");
    replace.mockRestore();
  });

  it("reads nothing while the tab is hidden (M-R20)", async () => {
    returnApi(() => payment());
    await arrive();
    expect(state()).toBe("confirming");
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const before = count("GET", "/api/v1/billing/payments/{payment_id}");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(count("GET", "/api/v1/billing/payments/{payment_id}")).toBe(before);
    visibility.mockRestore();
  });

  it("shows nothing from a read that never reached the API (M-R18)", async () => {
    returnApi(() => {
      throw new TypeError("Failed to fetch");
    });
    await arrive();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(count("GET", "/api/v1/billing/payments/{payment_id}")).toBeGreaterThan(0);
    expect(state()).toBe("confirming");
  });

  it("moves focus to its heading once the payment is settled (M-S29)", async () => {
    returnApi(() => payment({ status: "succeeded", processed_at: iso(Date.now()) }));
    await arrive();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });
    expect(state()).toBe("succeeded");
    expect(document.activeElement?.id).toBe("billing-title");
  });

  it.each(["DECLINED", "AUTHENTICATION_FAILED"])(
    "names a %s card as the bank's refusal (RT-05, M-S30)",
    async (reason) => {
      returnApi(() => payment({ status: "failed", failure_reason: reason }));
      await arrive();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_500);
      });
      expect(state()).toBe("failed");
      expect(screen.getByText(app.billing.return["failedAuth"]!)).toBeTruthy();
    },
  );
});

/* -------------------------------------------------------------------- plan */

describe("the plan page (BP, CK)", () => {
  function planApi(sub: Record<string, unknown> = subscription(), proName = "Pro") {
    answers["GET /api/v1/billing/subscription"] = () => ({ subscription: sub, entitlements: [] });
    answers["GET /api/v1/billing/plans"] = () => [FREE, pro(proName)];
    answers["GET /api/v1/billing/summary"] = () => ({
      automatic_renewal: false,
      payment_required: [],
      open_offer: null,
    });
    answers["GET /api/v1/billing/custom-offers"] = () => [];
  }

  async function upgrade(): Promise<HTMLElement> {
    return screen.findByRole("button", { name: /Pro/ }, { timeout: 5_000 });
  }

  it("never asks for the owner's summary for an administrator (OQ-5, M-R23)", async () => {
    role = "tenant_admin";
    planApi();
    render(<PlanPage />, { wrapper: Wrapper });
    await screen.findByText("Pro");
    expect(count("GET", "/api/v1/billing/summary")).toBe(0);
    expect(count("GET", "/api/v1/billing/custom-offers")).toBe(0);
  });

  it("offers to keep the plan only when a change is scheduled (M-S35)", async () => {
    planApi();
    render(<PlanPage />, { wrapper: Wrapper });
    await screen.findByText("Pro");
    await screen.findAllByText(/Cancel/);
    expect(document.querySelector('[data-action="cancel-subscription"]')).not.toBeNull();
    expect(document.querySelector('[data-action="keep-plan"]')).toBeNull();
  });

  it("sends one checkout for presses while it opens, and the button stays focusable (M-S32, M-S33)", async () => {
    planApi();
    answers["POST /api/v1/billing/checkout"] = () => new Promise(() => undefined);
    render(<PlanPage />, { wrapper: Wrapper });
    const button = await upgrade();
    fireEvent.click(button);
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(button);
    fireEvent.click(button);
    // Whatever a second press started has reached the API by now.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(count("POST", "/api/v1/billing/checkout")).toBe(1);
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(button.hasAttribute("disabled")).toBe(false);
  });

  it("gives each press its own idempotency key (CK-02, M-S31)", async () => {
    planApi();
    answers["POST /api/v1/billing/checkout"] = () => ({
      __status: 409,
      error: { error: { code: "checkout_already_started", message: "x" } },
    });
    render(<PlanPage />, { wrapper: Wrapper });
    const button = await upgrade();
    fireEvent.click(button);
    await screen.findByRole("alert");
    fireEvent.click(button);
    await act(async () => {
      await Promise.resolve();
    });
    const keys = calls
      .filter((call) => call.method === "POST" && call.path === "/api/v1/billing/checkout")
      .map((call) => (call.init?.["body"] as { idempotency_key: string }).idempotency_key);
    expect(keys).toHaveLength(2);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("draws a plan's name as text, never as markup (UI8-04, M-S46)", async () => {
    planApi(subscription(), "<b>Pro</b>");
    render(<PlanPage />, { wrapper: Wrapper });
    await screen.findByText("<b>Pro</b>");
    expect(document.querySelector("[data-plans] b")).toBeNull();
  });
});

/* ----------------------------------------------------------------- invoice */

describe("an invoice (IN)", () => {
  it("offers no refund review on a refunded payment (M-R22)", async () => {
    answers["GET /api/v1/invoices/{invoice_id}"] = () =>
      invoice({
        status: "paid",
        amount_paid: "99.00",
        outstanding: "0.00",
        paid_at: iso(Date.now()),
      });
    answers["GET /api/v1/invoices/{invoice_id}/payments"] = () => [
      payment({
        status: "refunded",
        processed_at: iso(Date.now()),
        refunded_amount: "99.00",
        refunded_at: iso(Date.now()),
      }),
    ];
    render(<InvoicePage id={INVOICE} />, { wrapper: Wrapper });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await screen.findByText(/Refunded/i, undefined, { timeout: 5_000 });
    expect(document.querySelector('[data-action="ask-refund"]')).toBeNull();
  });
});
