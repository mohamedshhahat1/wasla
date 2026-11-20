/**
 * The return page's rules (RT-02 .. RT-05), pure so they are proved with an injected clock.
 *
 * What decides is ONLY the payment the API reads (`PaymentRead.status`) - never the redirect's
 * query string, which says success=true while a payment is pending or declined (P02, P54). Success
 * is shown only when the status is `succeeded`.
 */

export const FAST_POLL_MS = 2_000;
export const FAST_WINDOW_MS = 60_000;
export const SLOW_POLL_MS = 10_000;
/** How far back "the payment you just started" is looked for (OQ-4). */
export const RECENT_WINDOW_MS = 2 * 60 * 60 * 1_000;

export type ReturnView =
  | { kind: "finding" }
  | { kind: "none" }
  | { kind: "confirming"; slow: boolean }
  | { kind: "succeeded" }
  | { kind: "failed" }
  | { kind: "refunded" }
  | { kind: "elsewhere" };

export type Found = "finding" | "none" | "found" | "elsewhere";

/** The page's state from the latest answered read; a read that never reached the API is not one. */
export function returnView(input: {
  found: Found;
  status: string | null;
  elapsedMs: number;
}): ReturnView {
  if (input.found === "finding") return { kind: "finding" };
  if (input.found === "none") return { kind: "none" };
  if (input.found === "elsewhere") return { kind: "elsewhere" };
  switch (input.status) {
    case "succeeded":
      return { kind: "succeeded" };
    case "failed":
      return { kind: "failed" };
    case "refunded":
      return { kind: "refunded" };
    default:
      return { kind: "confirming", slow: input.elapsedMs >= FAST_WINDOW_MS };
  }
}

/** The wait before the next read: every 2 s for 60 s, then every 10 s; none once settled. */
export function nextPollDelay(status: string | null, elapsedMs: number): number | null {
  if (status === "succeeded" || status === "failed" || status === "refunded") return null;
  return elapsedMs < FAST_WINDOW_MS ? FAST_POLL_MS : SLOW_POLL_MS;
}

export interface InvoiceFacts {
  id: string;
  purpose: string;
  issued_at: string | null;
}

/**
 * The purposes a hosted payment is opened for: a purchase (a plan, or a custom offer accepted),
 * a renewal paid by checkout, a top-up. `manual` and `adjustment` are never paid by the owner.
 */
const PAYABLE_PURPOSES = new Set(["checkout", "renewal", "topup"]);

/**
 * The invoices issued in the last two hours that a hosted payment could be for, newest first
 * (RT-02). The page follows the first; any others are listed with links to their invoices.
 */
export function recentPayableInvoices<T extends InvoiceFacts>(
  invoices: readonly T[],
  now: number,
): T[] {
  return invoices
    .filter((invoice) => {
      if (!PAYABLE_PURPOSES.has(invoice.purpose) || !invoice.issued_at) return false;
      const at = Date.parse(invoice.issued_at);
      return Number.isFinite(at) && now - at <= RECENT_WINDOW_MS && at - now <= 60_000;
    })
    .sort((a, b) => Date.parse(b.issued_at ?? "") - Date.parse(a.issued_at ?? ""));
}

/** The newest attempt on one invoice, or null when there is none. */
export function newestPayment<T extends { created_at: string }>(payments: readonly T[]): T | null {
  let newest: T | null = null;
  for (const payment of payments) {
    if (!newest || Date.parse(payment.created_at) > Date.parse(newest.created_at)) {
      newest = payment;
    }
  }
  return newest;
}

/** A card's masked digits as the API gives them - and never more than the last four (PM-01). */
export function maskedDigits(masked: string | null | undefined): string | null {
  if (!masked) return null;
  const digits = masked.replace(/\D/g, "");
  if (digits.length === 0) return null;
  return digits.slice(-4);
}
