/**
 * A Coexistence or Embedded Signup number after onboarding (ADR-204): Meta's disconnect reasons,
 * the payment-method help link and home's warnings. Kept apart from the Embedded Signup flow module, which
 * names Meta's SDK, so the connection page and home never carry the SDK's URL (FGE-8).
 */

/** Meta's help page for adding a payment method in WhatsApp Manager (UD-06). */
export const META_PAYMENT_HELP = "https://www.facebook.com/business/help/488291839463771";

/** Provider disconnect reasons (EXT-ES-32) to sentence keys (UD-08); unknown → "unknown". */
export const DISCONNECT_REASONS: readonly string[] = [
  "PRIMARY_INACTIVITY",
  "COMPANION_INACTIVITY",
  "ACCOUNT_DISCONNECTED",
  "CHANGE_NUMBER",
  "BUSINESS_DOWNGRADE",
  "USER_RE_REGISTERED",
  "ACCOUNT_OFFBOARDED",
  "PARTNER_APP_UNINSTALLED",
];

export function disconnectReasonKey(reason: string | null | undefined): string {
  return reason && DISCONNECT_REASONS.includes(reason) ? reason : "unknown";
}

export interface NumberFacts {
  id: string;
  status: string;
  mode: string | null;
  reminderDays: number | null;
  health: string | null;
}

/**
 * HM9-01 / HM9-02: which of home's Coexistence warnings apply - the reminder for an active
 * Coexistence number, and the first number Meta disconnected. Never a reminder for a number that
 * is not Coexistence (M-R36).
 */
export function coexistenceWarnings(numbers: readonly NumberFacts[]): {
  reminder: { days: number } | null;
  disconnected: string | null;
} {
  const coexisting = numbers.find(
    (number) => number.status === "active" && number.mode === "coexistence",
  );
  const disconnected = numbers.find((number) => number.health === "provider_disconnected");
  return {
    reminder: coexisting ? { days: coexisting.reminderDays ?? 13 } : null,
    disconnected: disconnected ? disconnected.id : null,
  };
}
