/**
 * What "Choose" does for one plan price (BP-03, RS8-01): decided from the API's own rules, so the
 * page never sends a priced plan to POST /billing/subscription or a pricier one to …/plan - both
 * answer 402 `plan_requires_checkout`. The decimal strings are read as numbers only to order two
 * prices; if the API still answers 402, the page moves to checkout with the same price.
 */
export type PlanAction =
  /** This is the plan and price the workspace is on. */
  | { kind: "current" }
  /** No subscription and the plan is free: POST /billing/subscription. */
  | { kind: "start-free" }
  /** From a free plan to another free one: POST /billing/subscription/plan, now. */
  | { kind: "switch-now" }
  /** Cheaper or free from a paid plan: POST /billing/subscription/plan, at the period's end. */
  | { kind: "switch-at-period-end" }
  /** Pricier, or any priced plan without a live paid one: the hosted checkout. */
  | { kind: "checkout" };

export interface CurrentTerms {
  /** No subscription, or one that has ended. */
  none: boolean;
  planCode: string | null;
  priceId: string | null;
  /** What the current terms cost per period, as the API states it ("0.00" when free). */
  amount: string | null;
}

export interface TargetPrice {
  planCode: string;
  priceId: string | null;
  amount: string;
  billingRequired: boolean;
}

function isFreeAmount(amount: string | null): boolean {
  const value = Number(amount ?? "0");
  return !Number.isFinite(value) || value === 0;
}

export function planAction(current: CurrentTerms, target: TargetPrice): PlanAction {
  const free = !target.billingRequired || isFreeAmount(target.amount);
  if (
    !current.none &&
    current.planCode === target.planCode &&
    (target.priceId === null || current.priceId === null || current.priceId === target.priceId)
  ) {
    return { kind: "current" };
  }
  if (current.none) return free ? { kind: "start-free" } : { kind: "checkout" };
  const currentFree = isFreeAmount(current.amount);
  if (free) return currentFree ? { kind: "switch-now" } : { kind: "switch-at-period-end" };
  if (!currentFree && Number(target.amount) < Number(current.amount)) {
    return { kind: "switch-at-period-end" };
  }
  return { kind: "checkout" };
}

/** The subscription's state in words (BP-02): the key under app.billing.status.* */
export type SubscriptionWord =
  "trialing" | "active" | "pastDue" | "cancelling" | "changing" | "cancelled" | "ended" | "none";

export function subscriptionWord(
  subscription: {
    status: string;
    cancel_at_period_end: boolean;
    scheduled_change: unknown;
    ended_at: string | null;
  } | null,
): SubscriptionWord {
  if (!subscription) return "none";
  if (subscription.ended_at || subscription.status === "expired") return "ended";
  if (subscription.status === "cancelled") return "cancelled";
  if (subscription.status === "past_due") return "pastDue";
  if (subscription.cancel_at_period_end) return "cancelling";
  if (subscription.scheduled_change) return "changing";
  if (subscription.status === "trialing") return "trialing";
  return "active";
}

/** "Cancel now" is confirmed only by the workspace's address typed exactly (OQ-8). */
export function cancelNowConfirmed(typed: string, slug: string): boolean {
  return slug !== "" && typed === slug;
}
