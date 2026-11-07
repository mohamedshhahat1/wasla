import "@wasla/ui/fonts";
import "./globals.css";

import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { dirOf, isLocale } from "@wasla/i18n";

import { Providers } from "../components/providers";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("common");
  return { title: t("appName"), robots: { index: false, follow: false } };
}

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  interactiveWidget: "resizes-content",
};

/** <html lang dir> come from the server's locale choice, in the first bytes (IN-04). */
export default async function RootLayout({ children }: { children: ReactNode }) {
  const requested = await getLocale();
  const locale = isLocale(requested) ? requested : "en";
  const dir = dirOf(locale);
  return (
    <html lang={locale} dir={dir} data-app="web">
      <body>
        <NextIntlClientProvider locale={locale} messages={await getMessages()}>
          <Providers locale={locale} dir={dir}>
            {children}
          </Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
