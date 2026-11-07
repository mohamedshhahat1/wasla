import { cookies, headers } from "next/headers";
import { getRequestConfig } from "next-intl/server";

import { LOCALE_COOKIE, messagesFor, resolveLocale } from "@wasla/i18n";

/**
 * The locale is chosen on the server before first paint (IN-04): the wasla_locale cookie, then
 * Accept-Language among the four, then English. No locale in the URL (PD-11).
 */
export default getRequestConfig(async () => {
  const locale = resolveLocale(
    (await cookies()).get(LOCALE_COOKIE)?.value,
    (await headers()).get("accept-language"),
  );
  return { locale, messages: messagesFor(locale) };
});
