/**
 * Formatters (IN-05). Every one takes the locale and, for times, an explicit time zone - the
 * workspace's for business times - so nothing depends on the machine's zone.
 */
import type { Locale } from "./locales";

/**
 * Digits in data: Latin by default in every locale, including Arabic (FEQ-07's recommended
 * default); "native" asks for Arabic-Indic digits in Arabic. Named explicitly, because CLDR 48
 * (Node 24's ICU 78) already gives plain `ar` Latin digits while `ar-EG` gives Arabic-Indic.
 * One flag, one place.
 */
export type Digits = "latn" | "native";
export const DEFAULT_DIGITS: Digits = "latn";

const NATIVE_DIGITS: Partial<Record<Locale, string>> = { ar: "arab" };

function numberingOf(digits: Digits, locale: Locale): { numberingSystem?: string } {
  const system = digits === "latn" ? "latn" : NATIVE_DIGITS[locale];
  return system ? { numberingSystem: system } : {};
}

const DECIMAL = /^-?\d+(?:\.\d+)?$/;

/**
 * Formats a money amount that the API sends as a decimal STRING ("0.10", "1234567.89").
 * The string goes to Intl.NumberFormat as it is (ECMA-402 NumberFormat v3 formats a decimal
 * string exactly), so it is never parsed to a float.
 */
export function formatMoney(
  amount: string,
  currency: string,
  locale: Locale,
  digits: Digits = DEFAULT_DIGITS,
): string {
  if (!DECIMAL.test(amount)) throw new Error(`not a decimal amount: ${JSON.stringify(amount)}`);
  if (!/^[A-Z]{3}$/.test(currency)) throw new Error(`not an ISO 4217 code: ${currency}`);
  const formatter = new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    ...numberingOf(digits, locale),
  });
  // `format` accepts a string at runtime (Intl.NumberFormat v3); the DOM lib types say number.
  return formatter.format(amount as unknown as number);
}

export function formatNumber(value: number, locale: Locale, digits: Digits = DEFAULT_DIGITS) {
  return new Intl.NumberFormat(locale, numberingOf(digits, locale)).format(value);
}

export function formatDate(
  iso: string | Date,
  locale: Locale,
  timeZone: string,
  digits: Digits = DEFAULT_DIGITS,
): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeZone,
    ...numberingOf(digits, locale),
  }).format(new Date(iso));
}

export function formatDateTime(
  iso: string | Date,
  locale: Locale,
  timeZone: string,
  digits: Digits = DEFAULT_DIGITS,
): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
    ...numberingOf(digits, locale),
  }).format(new Date(iso));
}

const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

/** "3 minutes ago" / "in 2 days", relative to an injected `now` (never the wall clock here). */
export function formatRelative(
  then: string | Date,
  now: Date,
  locale: Locale,
  digits: Digits = DEFAULT_DIGITS,
): string {
  const seconds = Math.round((new Date(then).getTime() - now.getTime()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(locale, {
    numeric: "auto",
    ...numberingOf(digits, locale),
  });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return formatter.format(Math.trunc(seconds / size), unit);
    }
  }
  return formatter.format(0, "second");
}

/** Locale-aware case mapping (Turkish İ/i and I/ı, plan section 20). */
export function toUpper(text: string, locale: Locale): string {
  return text.toLocaleUpperCase(locale);
}

export function toLower(text: string, locale: Locale): string {
  return text.toLocaleLowerCase(locale);
}

/** A collator for sorting names in the reader's language. */
export function collator(locale: Locale): Intl.Collator {
  return new Intl.Collator(locale, { sensitivity: "base", numeric: true });
}
