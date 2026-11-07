/** The four locales (D-07, IN-01), their direction, and negotiation from Accept-Language. */

export const LOCALES = ["en", "ar", "fr", "tr"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

/** The cookie the apps read on the server before first paint (PD-11, IN-04). */
export const LOCALE_COOKIE = "wasla_locale";
export const LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

export type Direction = "ltr" | "rtl";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

export function dirOf(locale: Locale): Direction {
  return locale === "ar" ? "rtl" : "ltr";
}

/** The locale's own name, for the language switcher (never translated). */
export const LOCALE_NAMES: Record<Locale, string> = {
  en: "English",
  ar: "العربية",
  fr: "Français",
  tr: "Türkçe",
};

/**
 * Picks a locale from an Accept-Language header: highest q first, a region ("ar-EG") matches
 * its language, "*" and unknown languages are skipped; nothing usable gives English.
 */
export function negotiate(acceptLanguage: string | null | undefined): Locale {
  if (!acceptLanguage) return DEFAULT_LOCALE;
  const ranges = acceptLanguage
    .split(",")
    .map((part, index) => {
      const [tag = "", ...params] = part.trim().split(";");
      const q = params
        .map((p) => p.trim())
        .find((p) => p.startsWith("q="))
        ?.slice(2);
      const weight = q === undefined ? 1 : Number(q);
      return {
        tag: tag.trim().toLocaleLowerCase("en"),
        weight: Number.isFinite(weight) ? weight : 0,
        index,
      };
    })
    .filter((range) => range.tag && range.weight > 0)
    .sort((a, b) => b.weight - a.weight || a.index - b.index);
  for (const { tag } of ranges) {
    const language = tag.split("-")[0];
    if (isLocale(language)) return language;
  }
  return DEFAULT_LOCALE;
}

/** The locale from the cookie when valid, else negotiated from the header. */
export function resolveLocale(
  cookie: string | null | undefined,
  acceptLanguage: string | null | undefined,
): Locale {
  return isLocale(cookie) ? cookie : negotiate(acceptLanguage);
}
