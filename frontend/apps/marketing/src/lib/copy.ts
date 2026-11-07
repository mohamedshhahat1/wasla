import {
  dirOf,
  LOCALE_NAMES,
  LOCALES,
  messagesFor,
  type Locale,
  type MessageTree,
} from "@wasla/i18n";

/** A marketing or common string for a locale (plain strings in FE-1; English fills a gap). */
export function text(locale: Locale, key: string): string {
  let node: MessageTree | string | undefined = messagesFor(locale);
  for (const part of key.split(".")) {
    node = typeof node === "object" ? node[part] : undefined;
  }
  if (typeof node !== "string") throw new Error(`no message ${key}`);
  return node;
}

export interface Alternate {
  locale: Locale;
  name: string;
  href: string;
  dir: "ltr" | "rtl";
}

/** The four language versions of a page, for hreflang and the switcher. */
export function alternates(path = ""): Alternate[] {
  return LOCALES.map((locale) => ({
    locale,
    name: LOCALE_NAMES[locale],
    href: `/${locale}/${path}`,
    dir: dirOf(locale),
  }));
}

export const publicEnv = {
  environment: import.meta.env["PUBLIC_WASLA_ENV"] as string,
  apiOrigin: import.meta.env["PUBLIC_API_ORIGIN"] as string,
  appOrigin: import.meta.env["PUBLIC_APP_ORIGIN"] as string,
  marketingOrigin: import.meta.env["PUBLIC_MARKETING_ORIGIN"] as string,
};
