"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";

import type { Direction, Locale } from "@wasla/i18n";
import { ToastViewport, UiProvider } from "@wasla/ui";

import { SessionProvider } from "./session-provider";

/** TanStack Query, the design system's root with the server's dir, and the session (FE-2). */
export function Providers({
  locale,
  dir,
  children,
}: {
  locale: Locale;
  dir: Direction;
  children: ReactNode;
}) {
  const t = useTranslations("common");
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <UiProvider dir={dir} locale={locale}>
        <SessionProvider>{children}</SessionProvider>
        <ToastViewport label={t("notifications")} />
      </UiProvider>
    </QueryClientProvider>
  );
}
