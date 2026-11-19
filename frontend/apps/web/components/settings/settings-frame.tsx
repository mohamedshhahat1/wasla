"use client";

import NextLink from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { SettingsNav } from "@wasla/ui";

import { isAdminRole } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";

export type SettingsPage = "workspace" | "activity" | "close" | "account";

/**
 * The settings area (SW-01, LY7-01): its own navigation beside the page at a desktop width and a
 * wrapped row of links above it on a phone. Workspace and Activity for owners and administrators,
 * Close workspace for the owner, Account for everybody - an entry a role may not open is absent.
 */
export function SettingsFrame({
  current,
  children,
}: {
  current: SettingsPage;
  children: ReactNode;
}) {
  const t = useTranslations("app.settings");
  const { role } = useWorkspace();
  const admin = isAdminRole(role);
  const items = [
    ...(admin
      ? [
          { key: "workspace", href: "/settings/workspace", label: t("nav.workspace") },
          { key: "activity", href: "/settings/activity", label: t("nav.activity") },
        ]
      : []),
    ...(role === "tenant_owner"
      ? [{ key: "close", href: "/settings/workspace/close", label: t("nav.close") }]
      : []),
    { key: "account", href: "/settings/account", label: t("nav.account") },
  ];
  return (
    <div className="flex flex-col gap-6 md:flex-row md:items-start" data-settings="">
      <div className="md:w-nav md:shrink-0">
        <SettingsNav
          label={t("nav.label")}
          items={items}
          current={current}
          renderLink={({ href, children: content, ...rest }) => (
            <NextLink href={href} {...rest}>
              {content}
            </NextLink>
          )}
        />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-6">{children}</div>
    </div>
  );
}
