/**
 * The account's language and this device's (LC-06, FEB-09, ADR-201). The page renders in the
 * `wasla_locale` cookie's language, read on the server before first paint (FE-1). The account
 * carries a language too, so every device follows it:
 *
 * - the account has one and the device another → the device takes the account's, once;
 * - the account has none (every account before FE-7, every Google sign-up) → the account takes
 *   the language the person is using, once (OQ-2's default);
 * - they agree → nothing.
 *
 * "Once" is once per signed-in session in this tab: the decision never runs in a loop.
 */
import { isLocale, type Locale } from "@wasla/i18n";

export type LocaleStep =
  | { kind: "none" }
  | { kind: "follow-account"; locale: Locale }
  | { kind: "write-account"; locale: Locale };

export function reconcileLocale(
  accountLocale: string | null | undefined,
  deviceLocale: string,
): LocaleStep {
  if (!isLocale(deviceLocale)) return { kind: "none" };
  if (accountLocale == null) return { kind: "write-account", locale: deviceLocale };
  if (!isLocale(accountLocale) || accountLocale === deviceLocale) return { kind: "none" };
  return { kind: "follow-account", locale: accountLocale };
}

/** sessionStorage holds only that this tab already reconciled - never a language or a person. */
export const RECONCILED_KEY = "wasla_locale_reconciled";

export function alreadyReconciled(storage: Pick<Storage, "getItem"> | null): boolean {
  try {
    return storage?.getItem(RECONCILED_KEY) === "1";
  } catch {
    return false;
  }
}

export function markReconciled(storage: Pick<Storage, "setItem"> | null): void {
  try {
    storage?.setItem(RECONCILED_KEY, "1");
  } catch {
    // Private mode: the check runs again next time, and agrees then - harmless.
  }
}

/** Sets this device's language the way the switcher does: a form POST to /locale (AP-04). */
export function postDeviceLocale(locale: Locale, next: string): void {
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/locale";
  form.style.display = "none";
  for (const [name, value] of [
    ["locale", locale],
    ["next", next],
  ] as const) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
}
