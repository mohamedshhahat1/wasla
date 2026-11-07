"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import type { Direction, Locale } from "@wasla/i18n";
import { UiProvider } from "@wasla/ui";

/** TanStack Query (no queries in FE-1) and the design system's root, with the server's dir. */
export function Providers({
  locale,
  dir,
  children,
}: {
  locale: Locale;
  dir: Direction;
  children: ReactNode;
}) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <UiProvider dir={dir} locale={locale}>
        {children}
      </UiProvider>
    </QueryClientProvider>
  );
}
