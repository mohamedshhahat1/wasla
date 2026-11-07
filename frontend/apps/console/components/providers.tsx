"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import type { Direction, Locale } from "@wasla/i18n";
import { UiProvider } from "@wasla/ui";

/** TanStack Query with the console's rules (no queries in FE-1) and the design system root. */
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
        // Every staff read is audited (plan section 15): no refetch nobody asked for.
        defaultOptions: {
          queries: {
            retry: false,
            staleTime: Infinity,
            refetchOnWindowFocus: false,
            refetchOnReconnect: false,
          },
        },
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
