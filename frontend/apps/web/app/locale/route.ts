import { isLocale, LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE } from "@wasla/i18n";

import { publicEnv } from "../../lib/env";
import { safeNext } from "../../lib/safe-next";

/**
 * The language switcher's form target: sets wasla_locale (the only cookie FE-1 sets, AP-04) and
 * redirects back with 303. A preference, not a credential: SameSite=Lax, Path=/, one year,
 * Secure outside development.
 */
export async function POST(request: Request): Promise<Response> {
  const form = await request.formData().catch(() => null);
  const locale = form?.get("locale");
  const next = safeNext(form?.get("next") ?? null);
  const headers = new Headers({ Location: next, "Cache-Control": "no-store" });
  if (isLocale(locale)) {
    const secure = publicEnv().environment === "development" ? "" : "; Secure";
    headers.append(
      "Set-Cookie",
      `${LOCALE_COOKIE}=${locale}; Path=/; Max-Age=${LOCALE_COOKIE_MAX_AGE}; SameSite=Lax${secure}`,
    );
  }
  return new Response(null, { status: 303, headers });
}
