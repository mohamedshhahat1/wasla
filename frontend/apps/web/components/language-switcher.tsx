"use client";

import { usePathname } from "next/navigation";

import { isLocale, LOCALE_NAMES, LOCALES, type Locale } from "@wasla/i18n";
import { Button } from "@wasla/ui";

/**
 * A plain form posted to /locale, which sets wasla_locale and redirects back: works without
 * JavaScript, and page script never touches document.cookie (AP-04).
 *
 * FE-7 (LC-06): signed in, `beforeSubmit` first writes the account's language, so every device and
 * every e-mail follow the choice; the form then posts as before, whether or not that write did.
 */
export function LanguageSwitcher({
  current,
  label,
  beforeSubmit,
}: {
  current: Locale;
  label: string;
  beforeSubmit?: (locale: Locale) => Promise<unknown>;
}) {
  const pathname = usePathname();
  return (
    <form
      method="post"
      action="/locale"
      aria-label={label}
      onSubmit={(event) => {
        if (!beforeSubmit) return;
        const submitter = event.nativeEvent.submitter as HTMLButtonElement | null;
        const locale = submitter?.value;
        if (!isLocale(locale)) return;
        event.preventDefault();
        const form = event.currentTarget;
        void beforeSubmit(locale)
          .catch(() => undefined)
          .finally(() => {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "locale";
            input.value = locale;
            form.appendChild(input);
            form.submit();
          });
      }}
      className="flex flex-wrap gap-1 rounded-lg bg-surface p-1"
    >
      <input type="hidden" name="next" value={pathname} />
      {LOCALES.map((locale) => (
        <Button
          key={locale}
          type="submit"
          name="locale"
          value={locale}
          variant={locale === current ? "primary" : "ghost"}
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
