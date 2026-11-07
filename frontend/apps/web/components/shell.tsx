import { getLocale, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { isLocale } from "@wasla/i18n";
import { Button, Link } from "@wasla/ui";

import { publicEnv } from "../lib/env";
import { ApiHealth } from "./api-health";
import { LanguageSwitcher } from "./language-switcher";

/** APP-SHELL (signed out): name, language, a sign-in that is not there yet, the footer. */
export async function Shell({ children }: { children: ReactNode }) {
  const t = await getTranslations("shell");
  const common = await getTranslations("common");
  const requested = await getLocale();
  const locale = isLocale(requested) ? requested : "en";
  const env = publicEnv();
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:m-3 focus:rounded-md focus:bg-surface-raised focus:p-3"
      >
        {common("skipToContent")}
      </a>
      <header className="border-b border-border bg-surface-raised">
        <div className="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-4 px-5 py-3">
          <p className="m-0 text-xl font-semibold text-text">{common("appName")}</p>
          <div className="flex flex-wrap items-center gap-3">
            <LanguageSwitcher current={locale} label={common("languageLabel")} />
            <Button disabled aria-describedby="sign-in-soon">
              {t("signIn")}
            </Button>
            <span id="sign-in-soon" className="text-sm text-text-muted">
              {t("comingSoon")}
            </span>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-page flex-1 px-5 py-8">
        {children}
      </main>
      <footer className="border-t border-border bg-surface-sunken">
        <div className="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-3 px-5 py-5">
          <Link href={`${env.marketingOrigin}/${locale}/`}>{t("aboutWasla")}</Link>
          <ApiHealth />
        </div>
      </footer>
    </div>
  );
}
