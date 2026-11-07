"use client";

import { Direction, Toast, Tooltip } from "radix-ui";
import { createContext, useContext, type ReactNode } from "react";

export type Dir = "ltr" | "rtl";

const LocaleContext = createContext<string>("en");

/** The locale UiProvider was given (for components that fold or compare text). */
export function useUiLocale(): string {
  return useContext(LocaleContext);
}

export interface UiProviderProps {
  /** The reading direction chosen on the server (IN-04); never flipped on the client. */
  dir: Dir;
  /** The BCP 47 locale, for components that format or fold text. */
  locale: string;
  children: ReactNode;
}

/**
 * The design system's root (DS-03): Radix's Direction.Provider so every primitive inherits the
 * reading direction, the shared Tooltip and Toast providers. Rendered once per app root.
 */
export function UiProvider({ dir, locale, children }: UiProviderProps) {
  return (
    <Direction.Provider dir={dir}>
      <Tooltip.Provider delayDuration={300}>
        <Toast.Provider swipeDirection={dir === "rtl" ? "left" : "right"} duration={6000}>
          <LocaleContext.Provider value={locale}>{children}</LocaleContext.Provider>
        </Toast.Provider>
      </Tooltip.Provider>
    </Direction.Provider>
  );
}
