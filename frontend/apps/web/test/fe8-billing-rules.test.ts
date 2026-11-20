/**
 * FE-8's pure billing rules (ADR-202): what "Choose" does for a plan (BP-03 - never a priced plan
 * to /subscription, never a pricier one to /subscription/plan), the subscription's state in words,
 * the "cancel now" confirmation, the hosted checkout's URL check (CK-03), the return page's view
 * and polling schedule (RT-04, RT-05 - success only when the API says `succeeded`), which payment
 * it follows (RT-02), the card's digits (PM-01) and money as the API states it.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { accountModes, signInFacts } from "../lib/account/methods";
import { checkoutUrlAllowed, newAttemptKey } from "../lib/billing/checkout";
import { formatMoney, isZero } from "../lib/billing/money";
import {
  FAST_POLL_MS,
  FAST_WINDOW_MS,
  maskedDigits,
  CANDIDATE_INVOICES,
  newestPayment,
  newestRecentPayment,
  nextPollDelay,
  payableCandidates,
  RECENT_WINDOW_MS,
  returnView,
  SLOW_POLL_MS,
} from "../lib/billing/payment-return";
import { cancelNowConfirmed, planAction, subscriptionWord } from "../lib/billing/plans";
import { mayChangeRole, memberControls, otherRole, type TenantRole } from "../lib/team/rules";

const onFree = { none: false, planCode: "starter", priceId: "p-starter", amount: "0.00" };
const onPro = { none: false, planCode: "pro", priceId: "p-pro-m", amount: "99.00" };
const nothing = { none: true, planCode: null, priceId: null, amount: null };

describe("planAction (BP-03)", () => {
  it("marks the current plan and price", () => {
    expect(
      planAction(onPro, {
        planCode: "pro",
        priceId: "p-pro-m",
        amount: "99.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "current" });
  });

  it("starts a free plan when there is no subscription, and sends a priced one to checkout", () => {
    expect(
      planAction(nothing, {
        planCode: "starter",
        priceId: null,
        amount: "0.00",
        billingRequired: false,
      }),
    ).toEqual({ kind: "start-free" });
    expect(
      planAction(nothing, {
        planCode: "pro",
        priceId: "p-pro-m",
        amount: "99.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "checkout" });
  });

  it("never sends a priced plan anywhere but checkout from a free one", () => {
    expect(
      planAction(onFree, {
        planCode: "pro",
        priceId: "p-pro-m",
        amount: "99.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "checkout" });
  });

  it("schedules a cheaper or free plan from a paid one at the period's end", () => {
    expect(
      planAction(onPro, {
        planCode: "starter",
        priceId: "p-s",
        amount: "0.00",
        billingRequired: false,
      }),
    ).toEqual({ kind: "switch-at-period-end" });
    expect(
      planAction(onPro, {
        planCode: "lite",
        priceId: "p-l",
        amount: "49.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "switch-at-period-end" });
  });

  it("sends a pricier plan, or another term of the same plan that costs more, to checkout", () => {
    expect(
      planAction(onPro, {
        planCode: "business",
        priceId: "p-b",
        amount: "299.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "checkout" });
    expect(
      planAction(onPro, {
        planCode: "pro",
        priceId: "p-pro-y",
        amount: "990.00",
        billingRequired: true,
      }),
    ).toEqual({ kind: "checkout" });
  });

  it("switches between free plans at once", () => {
    expect(
      planAction(onFree, { planCode: "free2", priceId: null, amount: "0", billingRequired: false }),
    ).toEqual({ kind: "switch-now" });
  });
});

describe("subscriptionWord (BP-02)", () => {
  const base = {
    status: "active",
    cancel_at_period_end: false,
    scheduled_change: null,
    ended_at: null,
  };
  it("names each state", () => {
    expect(subscriptionWord(null)).toBe("none");
    expect(subscriptionWord(base)).toBe("active");
    expect(subscriptionWord({ ...base, status: "trialing" })).toBe("trialing");
    expect(subscriptionWord({ ...base, status: "past_due" })).toBe("pastDue");
    expect(subscriptionWord({ ...base, cancel_at_period_end: true })).toBe("cancelling");
    expect(subscriptionWord({ ...base, scheduled_change: { plan: "starter" } })).toBe("changing");
    expect(subscriptionWord({ ...base, status: "cancelled" })).toBe("cancelled");
    expect(subscriptionWord({ ...base, ended_at: "2026-11-01T00:00:00Z" })).toBe("ended");
  });
});

describe("cancel now (OQ-8)", () => {
  it("is confirmed only by the exact address", () => {
    expect(cancelNowConfirmed("acme", "acme")).toBe(true);
    expect(cancelNowConfirmed("Acme", "acme")).toBe(false);
    expect(cancelNowConfirmed(" acme", "acme")).toBe(false);
    expect(cancelNowConfirmed("", "")).toBe(false);
  });
});

describe("checkoutUrlAllowed (CK-03)", () => {
  it("allows an absolute https URL", () => {
    expect(
      checkoutUrlAllowed("https://accept.paymob.com/unifiedcheckout/?x=1", { e2eHooks: false }),
    ).toBe(true);
  });
  it("refuses http outside the e2e build, and anything relative, scripted or credentialed", () => {
    for (const raw of [
      "http://accept.paymob.com/x",
      "http://localhost:8026/paymob/?clientSecret=a",
      "/billing",
      "javascript:alert(1)",
      "data:text/html,hi",
      "https://user:pass@accept.paymob.com/",
      "",
      null,
      42,
    ]) {
      expect(checkoutUrlAllowed(raw, { e2eHooks: false })).toBe(false);
    }
  });
  it("admits the fake Paymob page on localhost only in the e2e build", () => {
    expect(
      checkoutUrlAllowed("http://localhost:8026/paymob/?clientSecret=a", { e2eHooks: true }),
    ).toBe(true);
    expect(checkoutUrlAllowed("http://127.0.0.1.example.com/paymob/", { e2eHooks: true })).toBe(
      false,
    );
    expect(checkoutUrlAllowed("http://evil.test/paymob/", { e2eHooks: true })).toBe(false);
  });
  it("draws a new key for each attempt", () => {
    let n = 0;
    const next = () => `key-${(n += 1)}`;
    expect(newAttemptKey(next)).not.toBe(newAttemptKey(next));
  });
});

describe("the return page (RT-04, RT-05)", () => {
  it("shows success only for a succeeded payment", () => {
    for (const status of ["pending", null, "unknown", "failed", "refunded"]) {
      for (const elapsedMs of [0, 5_000, FAST_WINDOW_MS, 10 * FAST_WINDOW_MS]) {
        expect(returnView({ found: "found", status, elapsedMs }).kind).not.toBe("succeeded");
      }
    }
    expect(returnView({ found: "found", status: "succeeded", elapsedMs: 0 })).toEqual({
      kind: "succeeded",
    });
  });

  it("reads a pending payment with a reason as a decline - never success, still read (BILL-07)", () => {
    expect(
      returnView({
        found: "found",
        status: "pending",
        failureReason: "AUTHENTICATION_FAILED",
        elapsedMs: 0,
      }),
    ).toEqual({ kind: "declined" });
    expect(nextPollDelay("pending", 0)).toBe(FAST_POLL_MS);
    expect(
      returnView({ found: "found", status: "succeeded", failureReason: "x", elapsedMs: 0 }),
    ).toEqual({ kind: "succeeded" });
  });

  it("confirms quietly for a minute, then says it may take longer", () => {
    expect(
      returnView({ found: "found", status: "pending", elapsedMs: FAST_WINDOW_MS - 1 }),
    ).toEqual({
      kind: "confirming",
      slow: false,
    });
    expect(returnView({ found: "found", status: "pending", elapsedMs: FAST_WINDOW_MS })).toEqual({
      kind: "confirming",
      slow: true,
    });
  });

  it("names a failure, a refund, no payment and another workspace's payment", () => {
    expect(returnView({ found: "found", status: "failed", elapsedMs: 0 }).kind).toBe("failed");
    expect(returnView({ found: "found", status: "refunded", elapsedMs: 0 }).kind).toBe("refunded");
    expect(returnView({ found: "none", status: null, elapsedMs: 0 }).kind).toBe("none");
    expect(returnView({ found: "elsewhere", status: null, elapsedMs: 0 }).kind).toBe("elsewhere");
    expect(returnView({ found: "finding", status: "succeeded", elapsedMs: 0 }).kind).toBe(
      "finding",
    );
  });

  it("reads every 2 s for a minute, then every 10 s, and stops once settled", () => {
    expect(nextPollDelay("pending", 0)).toBe(FAST_POLL_MS);
    expect(nextPollDelay("pending", FAST_WINDOW_MS - 1)).toBe(FAST_POLL_MS);
    expect(nextPollDelay("pending", FAST_WINDOW_MS)).toBe(SLOW_POLL_MS);
    expect(nextPollDelay(null, 0)).toBe(FAST_POLL_MS);
    for (const status of ["succeeded", "failed", "refunded"]) {
      expect(nextPollDelay(status, 0)).toBeNull();
    }
  });
});

describe("which payment the return page follows (RT-02, OQ-4)", () => {
  const now = Date.parse("2026-11-20T12:00:00Z");
  const at = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString();

  it("asks the newest payable invoices, whatever their dates say", () => {
    const invoices = [
      { id: "manual", purpose: "manual" },
      { id: "plan", purpose: "checkout" },
      { id: "topup", purpose: "topup" },
      { id: "adjust", purpose: "adjustment" },
      { id: "renewal", purpose: "renewal" },
      { id: "c2", purpose: "checkout" },
      { id: "c3", purpose: "checkout" },
      { id: "c4", purpose: "checkout" },
    ];
    expect(payableCandidates(invoices).map((i) => i.id)).toEqual([
      "plan",
      "topup",
      "renewal",
      "c2",
      "c3",
    ]);
    expect(CANDIDATE_INVOICES).toBe(5);
  });

  it("follows the newest attempt created in the last two hours", () => {
    expect(RECENT_WINDOW_MS).toBe(2 * 60 * 60 * 1_000);
    expect(
      newestRecentPayment(
        [
          { id: "old", created_at: at(121) },
          { id: "a", created_at: at(30) },
          { id: "b", created_at: at(2) },
        ],
        now,
      )?.id,
    ).toBe("b");
    expect(newestRecentPayment([{ id: "old", created_at: at(121) }], now)).toBeNull();
  });

  it("follows the newest attempt on the invoice", () => {
    expect(newestPayment([])).toBeNull();
    expect(
      newestPayment([
        { id: "a", created_at: at(5) },
        { id: "b", created_at: at(1) },
        { id: "c", created_at: at(3) },
      ])?.id,
    ).toBe("b");
  });
});

describe("a card's digits (PM-01)", () => {
  it("never shows more than the last four", () => {
    expect(maskedDigits("2346")).toBe("2346");
    expect(maskedDigits("512345xxxxxx2346")).toBe("2346");
    expect(maskedDigits("**** **** **** 2346")).toBe("2346");
    expect(maskedDigits(null)).toBeNull();
    expect(maskedDigits("xxxx")).toBeNull();
  });
});

describe("money as the API states it (UI8-04)", () => {
  it("formats the API's string with its currency", () => {
    expect(formatMoney("99.00", "EGP", "en")).toContain("99.00");
    expect(formatMoney("99.00", "EGP", "en")).toMatch(/EGP|E£/);
    expect(formatMoney("", "EGP", "en")).toBe("");
    expect(formatMoney("abc", "EGP", "en")).toBe("abc EGP");
    expect(isZero("0.00")).toBe(true);
    expect(isZero("99.00")).toBe(false);
  });
});

describe("the return page never reads the redirect's query string (RT-03)", () => {
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : [path];
    });
  }

  it("has no location.search or useSearchParams in its modules", () => {
    const root = join(__dirname, "..");
    const modules = [
      ...files(join(root, "app", "(app)", "billing", "checkout")),
      ...files(join(root, "components", "billing")).filter((path) => path.includes("return")),
      join(root, "lib", "billing", "payment-return.ts"),
    ];
    expect(modules.length).toBeGreaterThan(2);
    for (const path of modules) {
      const text = readFileSync(path, "utf8");
      expect(text, path).not.toMatch(/location\.search|useSearchParams|searchParams/);
    }
  });
});

describe("FEB-42's role change on /team (TM8-01)", () => {
  const row = (userId: string, role: TenantRole, status = "active") => ({ userId, role, status });
  const roles: TenantRole[] = ["tenant_owner", "tenant_admin", "member"];

  it("follows the API's rules for every caller and target", () => {
    const expected: Record<TenantRole, Record<TenantRole, boolean>> = {
      tenant_owner: { tenant_owner: false, tenant_admin: true, member: true },
      tenant_admin: { tenant_owner: false, tenant_admin: false, member: true },
      member: { tenant_owner: false, tenant_admin: false, member: false },
    };
    for (const viewer of roles) {
      for (const target of roles) {
        expect(
          memberControls({ userId: "me", role: viewer }, row("x", target), 2).changeRole,
          `${viewer} -> ${target}`,
        ).toBe(expected[viewer][target]);
      }
    }
  });

  it("is never offered on one's own row or a removed person's", () => {
    for (const viewer of roles) {
      expect(memberControls({ userId: "me", role: viewer }, row("me", viewer), 2).changeRole).toBe(
        false,
      );
      expect(
        memberControls({ userId: "me", role: viewer }, row("x", "member", "revoked"), 2).changeRole,
      ).toBe(false);
    }
  });

  it("offers the other role", () => {
    expect(otherRole("member")).toBe("tenant_admin");
    expect(otherRole("tenant_admin")).toBe("member");
    expect(mayChangeRole("tenant_owner", "tenant_owner")).toBe(false);
  });
});

describe("FEB-45's account modes (AC8-01 .. AC8-03)", () => {
  it("offers one password mode", () => {
    expect(accountModes({ hasPassword: true, googleConnected: false }, true).password).toBe(
      "change",
    );
    expect(accountModes({ hasPassword: false, googleConnected: true }, true).password).toBe("set");
  });

  it("offers connect or disconnect by the fact, and never a disconnect that locks the account", () => {
    expect(accountModes({ hasPassword: true, googleConnected: false }, true).google).toBe(
      "connect",
    );
    expect(accountModes({ hasPassword: true, googleConnected: false }, false).google).toBe(
      "hidden",
    );
    expect(accountModes({ hasPassword: true, googleConnected: true }, true).google).toBe(
      "disconnect",
    );
    expect(accountModes({ hasPassword: false, googleConnected: true }, true).google).toBe(
      "disconnect-blocked",
    );
  });

  it("offers exactly the proofs that can close the account", () => {
    expect(accountModes({ hasPassword: true, googleConnected: false }, true).closeProofs).toEqual([
      "password",
    ]);
    expect(accountModes({ hasPassword: false, googleConnected: true }, true).closeProofs).toEqual([
      "google",
    ]);
    expect(accountModes({ hasPassword: true, googleConnected: true }, true).closeProofs).toEqual([
      "password",
      "google",
    ]);
    expect(accountModes({ hasPassword: true, googleConnected: true }, false).closeProofs).toEqual([
      "password",
    ]);
  });

  it("reads the facts from a profile", () => {
    expect(signInFacts({ has_password: false, google_connected: true })).toEqual({
      hasPassword: false,
      googleConnected: true,
    });
    expect(signInFacts(null)).toEqual({ hasPassword: true, googleConnected: false });
  });
});
