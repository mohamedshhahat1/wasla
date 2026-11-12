import { getLocale, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { isLocale } from "@wasla/i18n";
import { Link, Wordmark } from "@wasla/ui";

import { publicEnv } from "../lib/env";
import { ApiHealth } from "./api-health";
import { LanguageSwitcher } from "./language-switcher";

/** The page frame until FE-3's shell: name, language, the footer (the session lives in the page). */
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
        className="sr-only focus:not-sr-only focus:m-3 focus:rounded-md focus:bg-surface-raised focus:p-3 focus:text-primary"
      >
        {common("skipToContent")}
      </a>
      <header className="bg-brand" data-brand-surface="">
        <div className="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-4 px-5 py-4">
          <p className="m-0">
            <Wordmark name={common("appName")} tone="inverse" />
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <LanguageSwitcher current={locale} label={common("languageLabel")} />
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-page flex-1 px-5 py-8 sm:py-12">
        {children}
      </main>
      <footer className="bg-primary" data-brand-surface="">
        <div className="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-3 px-5 py-5 sm:py-6">
          <Link tone="inverse" href={`${env.marketingOrigin}/${locale}/`}>
            {t("aboutWasla")}
          </Link>
          <ApiHealth />
        </div>
      </footer>
    </div>
  );
}
