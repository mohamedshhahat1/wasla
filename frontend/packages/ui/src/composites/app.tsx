"use client";

import { Menu, X, type LucideIcon } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { createContext, useContext, useState, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { IconButton } from "../primitives/icon-button";
import { focusRing } from "../primitives/styles";

/**
 * The signed-in frame (FE-3, UI3-02): the dark-blue top bar, the light-blue side navigation at
 * the inline start from 1024 px, a navigation sheet below that, the banner slot, the page and the
 * footer band. Presentational: every word, link and handler comes from the app (SH-01).
 */

/** Lets a navigation entry close the phone sheet when it is followed. */
const SheetContext = createContext<() => void>(() => undefined);

/** For NavList and the app: closes the navigation sheet (a no-op on a wide screen). */
export function useCloseNavigation(): () => void {
  return useContext(SheetContext);
}

export interface AppShellProps {
  /** "Skip to content" in the page's language - the first focusable element. */
  skipLabel: string;
  /** The wordmark (or the app's name) at the inline start of the top bar. */
  brand: ReactNode;
  /** The top bar's inline-end group: the switcher, the live indicator, the account menu. */
  barEnd?: ReactNode;
  /** The navigation's accessible name ("Main"). */
  navLabel: string;
  /** The navigation list (a NavList). */
  navigation: ReactNode;
  /** The phone menu button's name, the sheet's title and its close button's name. */
  menuLabel: string;
  closeLabel: string;
  /** What the sheet shows under the navigation on a phone (the account entries). */
  sheetFooter?: ReactNode;
  /** The banner area (a BannerStack); nothing is drawn when absent. */
  banners?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}

export function AppShell({
  skipLabel,
  brand,
  barEnd,
  navLabel,
  navigation,
  menuLabel,
  closeLabel,
  sheetFooter,
  banners,
  footer,
  children,
}: AppShellProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  return (
    <SheetContext.Provider value={() => setSheetOpen(false)}>
      <div className="flex min-h-dvh flex-col bg-canvas" data-app-shell="">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:m-3 focus:rounded-md focus:bg-surface-raised focus:p-3 focus:text-primary"
        >
          {skipLabel}
        </a>
        {/* Scrolls with the page (SH-02): a bar that stayed would have to be proved never to cover
            a focused field of every form under it; this one never covers anything. */}
        <header className="bg-brand text-on-brand" data-brand-surface="" data-app-bar="">
          <div className="flex min-h-target flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 lg:px-6">
            <DialogPrimitive.Root open={sheetOpen} onOpenChange={setSheetOpen}>
              <DialogPrimitive.Trigger asChild>
                <IconButton
                  icon={Menu}
                  iconName="Menu"
                  label={menuLabel}
                  data-nav-trigger=""
                  tone="inverse"
                  className="lg:hidden"
                />
              </DialogPrimitive.Trigger>
              <DialogPrimitive.Portal>
                <DialogPrimitive.Overlay className="wasla-scrim fixed inset-0 z-(--z-overlay) bg-scrim lg:hidden" />
                <DialogPrimitive.Content
                  aria-describedby={undefined}
                  data-nav-sheet=""
                  className={cn(
                    "wasla-sheet fixed inset-y-0 start-0 z-(--z-modal) flex w-full max-w-nav flex-col",
                    "overflow-y-auto border-e border-border bg-secondary-subtle shadow-lg lg:hidden",
                  )}
                >
                  <div
                    className="flex items-center justify-between gap-3 bg-brand px-4 py-3"
                    data-brand-surface=""
                  >
                    <DialogPrimitive.Title className="m-0 text-lg font-semibold text-on-brand">
                      {menuLabel}
                    </DialogPrimitive.Title>
                    <DialogPrimitive.Close asChild>
                      <IconButton icon={X} iconName="X" label={closeLabel} tone="inverse" />
                    </DialogPrimitive.Close>
                  </div>
                  <nav aria-label={navLabel} className="flex flex-col gap-2 p-4">
                    {navigation}
                  </nav>
                  {sheetFooter ? (
                    <div className="mt-auto border-t border-border p-4">{sheetFooter}</div>
                  ) : null}
                </DialogPrimitive.Content>
              </DialogPrimitive.Portal>
            </DialogPrimitive.Root>
            <div className="flex min-w-0 items-center">{brand}</div>
            {barEnd ? (
              <div className="ms-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
                {barEnd}
              </div>
            ) : null}
          </div>
        </header>
        <div className="flex flex-1">
          <nav
            aria-label={navLabel}
            data-side-nav=""
            className="hidden w-nav shrink-0 flex-col gap-2 border-e border-border bg-secondary-subtle p-4 lg:flex"
          >
            {navigation}
          </nav>
          <div className="flex min-w-0 flex-1 flex-col">
            {banners}
            <main
              id="main"
              tabIndex={-1}
              className="flex-1 px-4 py-6 outline-none sm:px-6 lg:px-8 lg:py-8"
            >
              <div className="mx-auto flex w-full max-w-page flex-col gap-6">{children}</div>
            </main>
            {footer}
          </div>
        </div>
      </div>
    </SheetContext.Provider>
  );
}

export interface NavItem {
  key: string;
  href: string;
  label: string;
  icon: LucideIcon;
  iconName: string;
  current?: boolean;
}

/**
 * The navigation entries. `renderLink` lets the app use its router's link; the default is a plain
 * anchor. The current entry carries aria-current="page" and the white "selected" surface.
 */
export function NavList({
  items,
  renderLink,
}: {
  items: NavItem[];
  renderLink?: (props: {
    href: string;
    className: string;
    children: ReactNode;
    "aria-current"?: "page";
    onClick: () => void;
  }) => ReactNode;
}) {
  const close = useCloseNavigation();
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0" data-nav-list="">
      {items.map((item) => {
        const className = cn(
          "flex min-h-target items-center gap-3 rounded-md px-4 text-md font-medium no-underline",
          item.current
            ? "bg-surface text-primary shadow-sm"
            : "text-text hover:bg-surface hover:text-primary",
          focusRing,
        );
        const children = (
          <>
            <Icon icon={item.icon} name={item.iconName} />
            <span>{item.label}</span>
          </>
        );
        const props = {
          href: item.href,
          className,
          children,
          onClick: close,
          ...(item.current ? { "aria-current": "page" as const } : {}),
        };
        return (
          <li key={item.key}>
            {renderLink ? (
              renderLink(props)
            ) : (
              <a
                href={props.href}
                className={props.className}
                onClick={props.onClick}
                aria-current={props["aria-current"]}
              >
                {children}
              </a>
            )}
          </li>
        );
      })}
    </ul>
  );
}
