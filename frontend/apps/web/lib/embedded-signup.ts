/**
 * Meta's Embedded Signup in the browser (ADR-204): the pure half - which origin may speak, what a
 * message event means, which sentence an error gets. The SDK and the popup live in
 * components/app/channels/embedded-signup.tsx; everything that can be decided without them is
 * decided here, with a table test.
 *
 * Meta's facts (implementation, version-5, errors; read 2026-11-21): the SDK is
 * https://connect.facebook.net/en_US/sdk.js; the session-logging listener accepts only an origin
 * ending in facebook.com and a JSON message whose `type` is WA_EMBEDDED_SIGNUP; FINISH and
 * FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING carry the session's ids (v5: messaging_account_ids and
 * is_on_biz_app; v4: waba_id); CANCEL carries current_step; ERROR carries error_code and
 * session_id; the code reaches the login callback only on Finish and lives 5 minutes.
 */

export const META_SDK_URL = "https://connect.facebook.net/en_US/sdk.js";
/** Meta's listener rule, made exact: facebook.com itself or a subdomain of it (SD-05). */
export const META_ORIGIN_SUFFIX = "facebook.com";
/** EXT-ES-09: the exchangeable code's time to live. */
export const CODE_LIFETIME_MS = 5 * 60 * 1000;
/** Meta's help page for adding a payment method in WhatsApp Manager (UD-06). */
export const META_PAYMENT_HELP = "https://www.facebook.com/business/help/488291839463771";

/**
 * Where the SDK comes from and which origin may post its events - Meta's in every build except
 * the e2e one, whose values come from next.config.ts at build time (SD-09): never a runtime switch.
 */
export interface SdkSource {
  url: string;
  /** null: Meta's rule (an origin ending in facebook.com); otherwise the one origin allowed. */
  origin: string | null;
}

export function sdkSource(): SdkSource {
  const fake = process.env.PUBLIC_WASLA_META_FAKE_ORIGIN;
  if (fake && fake !== "none") return { url: `${fake}/meta/sdk.js`, origin: fake };
  return { url: META_SDK_URL, origin: null };
}

/** SD-05: Meta's `event.origin.endsWith('facebook.com')`, without accepting evilfacebook.com. */
export function trustedOrigin(origin: string, source: SdkSource): boolean {
  if (source.origin !== null) return origin === source.origin;
  let host: string;
  try {
    const url = new URL(origin);
    if (url.protocol !== "https:") return false;
    host = url.hostname;
  } catch {
    return false;
  }
  return host === META_ORIGIN_SUFFIX || host.endsWith(`.${META_ORIGIN_SUFFIX}`);
}

export type SessionEvent =
  | {
      kind: "finish";
      onBusinessApp: boolean;
      phoneNumberId: string;
      messagingAccountId: string;
      businessId: string | null;
    }
  | { kind: "cancel"; step: string | null }
  | { kind: "error"; code: number | null; sessionId: string | null };

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * One message event as Embedded Signup's session info (ST-02), or null for anything else: a
 * foreign origin, text that is not JSON, another type, a FINISH without the ids. v5 and v4 both.
 */
export function readSessionEvent(
  origin: string,
  data: unknown,
  source: SdkSource,
): SessionEvent | null {
  if (!trustedOrigin(origin, source)) return null;
  let parsed: unknown;
  try {
    parsed = typeof data === "string" ? JSON.parse(data) : data;
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const message = parsed as { type?: unknown; event?: unknown; data?: unknown };
  if (message.type !== "WA_EMBEDDED_SIGNUP") return null;
  const body = (message.data && typeof message.data === "object" ? message.data : {}) as Record<
    string,
    unknown
  >;
  if (message.event === "FINISH" || message.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") {
    const phoneNumberId = text(body.phone_number_id);
    const accounts = Array.isArray(body.messaging_account_ids) ? body.messaging_account_ids : [];
    const messagingAccountId = text(accounts[0]) ?? text(body.waba_id);
    if (!phoneNumberId || !messagingAccountId) return null;
    return {
      kind: "finish",
      onBusinessApp:
        message.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING" || body.is_on_biz_app === true,
      phoneNumberId,
      messagingAccountId,
      businessId: text(body.business_id),
    };
  }
  if (message.event === "CANCEL") return { kind: "cancel", step: text(body.current_step) };
  if (message.event === "ERROR") {
    const code = typeof body.error_code === "number" ? body.error_code : Number(body.error_code);
    return {
      kind: "error",
      code: Number.isFinite(code) ? code : null,
      sessionId: text(body.session_id),
    };
  }
  return null;
}

/** ST-04: the screens of S6's "Abandoned flow screens", as words a person reads. */
export const CANCEL_STEPS: Readonly<Record<string, string>> = {
  ASSET_SELECTION: "chooseAccount",
  BUSINESS_ACCOUNT_SELECTION: "chooseAccount",
  ASSET_CREATION: "createAccount",
  PHONE_NUMBER_SETUP: "addNumber",
  WABA_PHONE_PROFILE_PICKER: "addNumber",
  PHONE_NUMBER_VERIFICATION: "verifyNumber",
  WHATSAPP_BUSINESS_PROFILE_SETUP: "profile",
  PERMISSIONS: "permissions",
  SAVE: "save",
  WHATSAPP_BUSINESS_APP_ONBOARDING_PHONE_CREATION: "addNumber",
  WHATSAPP_BUSINESS_APP_ONBOARDING_QR_CODE: "qrCode",
  WHATSAPP_BUSINESS_APP_ONBOARDING_PHONE_VERIFICATION: "verifyNumber",
  WHATSAPP_BUSINESS_APP_ONBOARDING_ACCOUNT_VERIFICATION: "verifyAccount",
};

export function cancelStepKey(step: string | null): string {
  return (step && CANCEL_STEPS[step]) || "unknown";
}

/**
 * EM-01: Meta's documented Embedded Signup error codes (errors, read 2026-11-21) that a person can
 * act on, each to a sentence key; any other code gets the generic sentence. Never a code Meta's
 * page does not list.
 */
export const ERROR_SENTENCES: Readonly<Record<number, string>> = {
  3441042: "region",
  200: "permission",
  2655111: "permission",
  3441107: "partnerNotReady",
  3441126: "partnerNotReady",
  3441072: "businessAppUnsupported",
  3441069: "numberOnWhatsApp",
  3441070: "numberOnWhatsApp",
  2494064: "numberOnWhatsApp",
  3441075: "twoStep",
  4563015: "updateApp",
  3441049: "waitAndRetry",
  2655094: "waitAndRetry",
  3441093: "waitAndRetry",
  3441145: "waitAndRetry",
  2655093: "otherPartner",
  2655049: "otherPartner",
  2494028: "otherPartner",
  3441091: "otherPartner",
  3441033: "tooManyTries",
  3441046: "tooManyTries",
  1675004: "tooManyTries",
  2388098: "accountLimit",
  2494067: "accountLimit",
  3441110: "numberLimit",
  2859009: "twoFactor",
  4612001: "twoFactor",
};

export function errorSentenceKey(code: number | null): string {
  return (code !== null && ERROR_SENTENCES[code]) || "generic";
}

/** ST-09: whether a code from Meta can still be sent, counted from the moment it arrived. */
export function codeStillFresh(receivedAt: number, now: number): boolean {
  return now - receivedAt < CODE_LIFETIME_MS;
}

/** SD-04: the `extras` FB.login is called with, from the API's configuration. */
export function loginExtras(esVersion: string, coexistence: boolean): Record<string, unknown> {
  if (esVersion === "v4") {
    return coexistence
      ? { setup: {}, featureType: "whatsapp_business_app_onboarding", sessionInfoVersion: "3" }
      : { setup: {} };
  }
  return coexistence
    ? { version: "v5", setup: {} }
    : {
        version: "v5",
        setup: {},
        features: [{ name: "whatsapp_business_app_onboarding", enabled: false }],
      };
}

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
