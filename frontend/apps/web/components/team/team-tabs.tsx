"use client";

import NextLink from "next/link";
import { useTranslations } from "next-intl";

import { SettingsNav } from "@wasla/ui";

import { isAdminRole } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";

/**
 * The team area's two pages as links (TI-01): Invitations only for owners and administrators -
 * a member never sees the link and the page is never requested for them.
 */
export function TeamTabs({ current }: { current: "members" | "invitations" }) {
  const t = useTranslations("app.team");
  const { role } = useWorkspace();
  const items = [
    { key: "members", href: "/team", label: t("tabs.members") },
    ...(isAdminRole(role)
      ? [{ key: "invitations", href: "/team/invitations", label: t("tabs.invitations") }]
      : []),
  ];
  if (items.length < 2) return null;
  return (
    <SettingsNav
      label={t("tabs.label")}
      items={items}
      current={current}
      renderLink={({ href, children, ...rest }) => (
        <NextLink href={href} {...rest}>
          {children}
        </NextLink>
      )}
    />
  );
}
