"use client";

import NextLink from "next/link";
import { useTranslations } from "next-intl";

import { linkClasses } from "@wasla/ui";

import { useWorkspace } from "../app/workspace-context";

/**
 * After a plan-limit sentence (BL-02): the owner gets the way to the plans; an administrator is
 * told who can change the plan; a member gets nothing new.
 */
export function PlanLimitLink({ href = "/billing" }: { href?: string }) {
  const t = useTranslations("app.team");
  const { role } = useWorkspace();
  if (role === "tenant_owner") {
    return (
      <>
        {" "}
        <NextLink href={href} className={linkClasses("text-sm")} data-action="see-plans">
          {t("billingLink")}
        </NextLink>
      </>
    );
  }
  if (role === "tenant_admin") return <> {t("askOwnerPlan")}</>;
  return null;
}
