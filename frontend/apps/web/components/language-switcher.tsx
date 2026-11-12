"use client";

import { usePathname } from "next/navigation";

import { LOCALE_NAMES, LOCALES, type Locale } from "@wasla/i18n";
import { Button } from "@wasla/ui";

/**
 * A plain form posted to /locale, which sets wasla_locale and redirects back: works without
 * JavaScript, and page script never touches document.cookie (AP-04).
 */
export function LanguageSwitcher({ current, label }: { current: Locale; label: string }) {
  const pathname = usePathname();
  return (
    <form method="post" action="/locale" aria-label={label} className="flex flex-wrap gap-1">
      <input type="hidden" name="next" value={pathname} />
      {LOCALES.map((locale) => (
        <Button
          key={locale}
          type="submit"
          name="locale"
          value={locale}
          variant={locale === current ? "secondary" : "ghost"}
          className="px-4 text-sm"
          aria-pressed={locale === current}
          lang={locale}
          data-locale={locale}
        >
          {LOCALE_NAMES[locale]}
        </Button>
      ))}
    </form>
  );
}
