/**
 * The catalogues as one object per locale. A key missing in ar, fr or tr falls back to English
 * at runtime (IN-02) - never to the key text - and `i18n:check` fails the build first.
 */
import arAuth from "../messages/ar/auth.json" with { type: "json" };
import arCommon from "../messages/ar/common.json" with { type: "json" };
import arErrors from "../messages/ar/errors.json" with { type: "json" };
import arMarketing from "../messages/ar/marketing.json" with { type: "json" };
import arShell from "../messages/ar/shell.json" with { type: "json" };
import enAuth from "../messages/en/auth.json" with { type: "json" };
import enCommon from "../messages/en/common.json" with { type: "json" };
import enErrors from "../messages/en/errors.json" with { type: "json" };
import enMarketing from "../messages/en/marketing.json" with { type: "json" };
import enShell from "../messages/en/shell.json" with { type: "json" };
import frAuth from "../messages/fr/auth.json" with { type: "json" };
import frCommon from "../messages/fr/common.json" with { type: "json" };
import frErrors from "../messages/fr/errors.json" with { type: "json" };
import frMarketing from "../messages/fr/marketing.json" with { type: "json" };
import frShell from "../messages/fr/shell.json" with { type: "json" };
import trAuth from "../messages/tr/auth.json" with { type: "json" };
import trCommon from "../messages/tr/common.json" with { type: "json" };
import trErrors from "../messages/tr/errors.json" with { type: "json" };
import trMarketing from "../messages/tr/marketing.json" with { type: "json" };
import trShell from "../messages/tr/shell.json" with { type: "json" };
import { DEFAULT_LOCALE, type Locale } from "./locales";

export const NAMESPACES = ["common", "errors", "shell", "marketing", "auth"] as const;
export type Namespace = (typeof NAMESPACES)[number];

export type MessageTree = { [key: string]: string | MessageTree };

const RAW: Record<Locale, Record<Namespace, Record<string, unknown>>> = {
  en: {
    common: enCommon,
    auth: enAuth,
    errors: enErrors,
    shell: enShell,
    marketing: enMarketing,
  },
  ar: {
    common: arCommon,
    auth: arAuth,
    errors: arErrors,
    shell: arShell,
    marketing: arMarketing,
  },
  fr: {
    common: frCommon,
    auth: frAuth,
    errors: frErrors,
    shell: frShell,
    marketing: frMarketing,
  },
  tr: {
    common: trCommon,
    auth: trAuth,
    errors: trErrors,
    shell: trShell,
    marketing: trMarketing,
  },
};

/** Drops `_meta` (review status) so it never reaches a screen. */
function strip(tree: Record<string, unknown>): MessageTree {
  const out: MessageTree = {};
  for (const [key, value] of Object.entries(tree)) {
    if (key === "_meta") continue;
    if (typeof value === "string") out[key] = value;
    else if (value && typeof value === "object") out[key] = strip(value as Record<string, unknown>);
  }
  return out;
}

/** `fallback` filled in under `primary` wherever primary lacks a key. */
export function withFallback(primary: MessageTree, fallback: MessageTree): MessageTree {
  const out: MessageTree = { ...fallback };
  for (const [key, value] of Object.entries(primary)) {
    const base = fallback[key];
    out[key] =
      typeof value === "object" && typeof base === "object" ? withFallback(value, base) : value;
  }
  return out;
}

/** The raw catalogue of one locale, `_meta` removed, no fallback. */
export function catalogue(locale: Locale, raw = RAW): Record<Namespace, MessageTree> {
  const sets = raw[locale];
  return {
    common: strip(sets.common),
    errors: strip(sets.errors),
    shell: strip(sets.shell),
    marketing: strip(sets.marketing),
    auth: strip(sets.auth),
  };
}

/** Every namespace of a locale, with English filling any missing key (IN-02). */
export function messagesFor(locale: Locale, raw = RAW): MessageTree {
  const own = catalogue(locale, raw) as unknown as MessageTree;
  if (locale === DEFAULT_LOCALE) return own;
  return withFallback(own, catalogue(DEFAULT_LOCALE, raw));
}

/** Whether each file of a locale was reviewed by a native speaker (FEQ-08). */
export function reviewStatus(locale: Locale): Record<Namespace, boolean> {
  const sets = RAW[locale];
  const reviewed = (ns: Namespace) =>
    Boolean((sets[ns]["_meta"] as { reviewed?: boolean } | undefined)?.reviewed);
  return {
    common: reviewed("common"),
    errors: reviewed("errors"),
    shell: reviewed("shell"),
    marketing: reviewed("marketing"),
    auth: reviewed("auth"),
  };
}

/**
 * The catalogue key for an API error code: "errors.<code>" when the catalogue has it,
 * otherwise "errors.fallback" (AC-03's messageKey is resolved here).
 */
export function errorMessageKey(code: string): string {
  const errors = catalogue(DEFAULT_LOCALE).errors;
  return Object.hasOwn(errors, code) ? `errors.${code}` : "errors.fallback";
}
