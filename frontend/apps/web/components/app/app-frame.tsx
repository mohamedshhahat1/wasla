"use client";

import NextLink from "next/link";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Suspense, type ReactNode } from "react";

import { AppShell, Link, LiveIndicator, NavList, Wordmark, type LiveState } from "@wasla/ui";

import { publicEnv } from "../../lib/env";
import { currentEntry, entriesFor } from "../../lib/navigation";
import type { SharedState } from "../../lib/stream-coordinator";
import { ApiHealth } from "../api-health";
import { PageGate } from "../page-gate";
import { useSession } from "../session-provider";
import { AccountMenu } from "./account-menu";
import { GateBanners, GateFullState } from "./gate-views";
import { WorkspaceProvider, useWorkspace } from "./workspace-context";
import { WorkspaceSwitcher } from "./workspace-switcher";

function liveOf(state: SharedState): LiveState {
  switch (state.kind) {
    case "open":
      return "live";
    case "connecting":
      return "connecting";
    case "waiting":
      return "paused";
    case "ended":
      return "down";
    case "stopped":
      return "off";
  }
}

function Frame({ children }: { children: ReactNode }) {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const common = useTranslations("common");
  const shell = useTranslations("shell");
  const locale = useLocale();
  const pathname = usePathname();
  const { role, workspaceName, decision, liveState } = useWorkspace();
  const current = currentEntry(pathname);
  const live = liveOf(liveState);
  const liveLabel =
    live === "live"
      ? t("live.live")
      : live === "connecting"
        ? t("live.connecting")
        : live === "paused"
          ? t("live.paused")
          : t("live.down");
  const roleLabel = role ? auth(`roles.${role}`) : "";
  return (
    <AppShell
      skipLabel={common("skipToContent")}
      brand={<Wordmark name={common("appName")} tone="inverse" />}
      barEnd={
        <>
          <WorkspaceSwitcher />
          {live === "off" ? null : <LiveIndicator state={live} label={liveLabel} />}
          <AccountMenu workspaceName={workspaceName} roleLabel={roleLabel} />
        </>
      }
      navLabel={t("navLabel")}
      navigation={
        <NavList
          items={entriesFor(role).map((entry) => ({
            key: entry.key,
            href: entry.route,
            label: t(`nav.${entry.labelKey}`),
            icon: entry.icon,
            iconName: entry.iconName,
            current: entry.key === current,
          }))}
          renderLink={({ href, children: content, ...rest }) => (
            <NextLink href={href} {...rest}>
              {content}
            </NextLink>
          )}
        />
      }
      menuLabel={t("menu")}
      closeLabel={t("close")}
      banners={decision.fullState ? null : <GateBanners />}
      footer={
        <footer className="bg-primary" data-brand-surface="">
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-5 sm:px-6 lg:px-8">
            <Link tone="inverse" href={`${publicEnv().marketingOrigin}/${locale}/`}>
              {shell("aboutWasla")}
            </Link>
            <ApiHealth />
          </div>
        </footer>
      }
    >
      {decision.fullState ? <GateFullState state={decision.fullState} /> : children}
    </AppShell>
  );
}

/** Keyed by workspace and session generation: a switch or a sign-in starts every gate afresh. */
function KeyedFrame({ children }: { children: ReactNode }) {
  const { workspace, generation } = useSession();
  return (
    <WorkspaceProvider key={`${workspace?.id ?? "none"}:${generation}`}>
      <Frame>{children}</Frame>
    </WorkspaceProvider>
  );
}

/**
 * The signed-in shell (APP-SHELL, SH-01): FE-2's routing decides first (signed out → /login,
 * unverified → /verify, no workspace → /workspaces/new); then the frame, the gates and the page.
 */
export function AppFrame({ children }: { children: ReactNode }) {
  return (
    <Suspense>
      <PageGate need="workspace">
        <KeyedFrame>{children}</KeyedFrame>
      </PageGate>
    </Suspense>
  );
}
