"use client";

/**
 * What the customers screens share (FE-5): who may do what, names for people, one minute clock per
 * screen, the lead-status badge, and the link every row is drawn with. No request is made here.
 */
import NextLink from "next/link";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError, type components } from "@wasla/api-client";
import { Badge, type StatusTone } from "@wasla/ui";

import { formatBudget } from "../../lib/customers/lead-form";
import { fullTime, relativeTime } from "../../lib/inbox/time";
import { appNow } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useMembers } from "../inbox/people";
import { useSession } from "../session-provider";

export type ContactRead = components["schemas"]["ContactRead"];
export type PickedContact = components["schemas"]["PickedContactRead"];
export type ConsentEvent = components["schemas"]["ConsentEventRead"];
export type LeadRead = components["schemas"]["LeadRead"];
export type LeadNote = components["schemas"]["LeadNoteRead"];
export type LeadActivity = components["schemas"]["LeadActivityRead"];

export const PAGE_SIZE = 30;

/** Lead assignment, clearing an opt-out and the statistics are an administrator's (Appendix A). */
export function useIsAdmin(): boolean {
  const { role } = useWorkspace();
  return role === "tenant_owner" || role === "tenant_admin";
}

/** One shared timer per screen for the relative times: at most once a minute. */
export function useMinuteClock(): number {
  const [now, setNow] = useState(() => appNow());
  useEffect(() => {
    const timer = setInterval(() => setNow(appNow()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** "5 minutes ago" and the full date behind it, in the page's locale. */
export function useTimes(): (
  iso: string | null | undefined,
) => { time: string; full: string } | null {
  const locale = useLocale();
  const now = useMinuteClock();
  return (iso) =>
    iso ? { time: relativeTime(iso, now, locale), full: fullTime(iso, locale) } : null;
}

/** A member's name by id: "You" for the viewer, the name, else "a former member". */
export function usePeople() {
  const t = useTranslations("app.customers");
  const members = useMembers();
  const { profile } = useSession();
  const nameOf = (id: string | null | undefined): string | null =>
    id
      ? id === profile?.id
        ? t("people.you")
        : (members.byId.get(id)?.name ?? t("people.former"))
      : null;
  return { members, nameOf, viewerId: profile?.id ?? null };
}

const STATUS_TONE: Record<string, StatusTone> = { won: "success", lost: "danger" };

/** A lead's status: the word, and a status colour only for the two outcomes (UI5-01). */
export function LeadStatusBadge({ status }: { status: string }) {
  const t = useTranslations("app.customers");
  const tone = STATUS_TONE[status] ?? "neutral";
  return (
    <Badge tone={tone} icon={tone !== "neutral"}>
      {t(`leadStatus.${status}` as "leadStatus.new")}
    </Badge>
  );
}

export function OptedOutBadge() {
  const t = useTranslations("app.customers");
  return <Badge tone="warning">{t("consent.optedOutBadge")}</Badge>;
}

/** The link a row is drawn with: Next's, so a row opens without a full page load. */
export function rowLink({
  href,
  className,
  children,
  ...rest
}: {
  href: string;
  className: string;
  children: ReactNode;
}) {
  return (
    <NextLink href={href} className={className} {...rest}>
      {children}
    </NextLink>
  );
}

export function useNumber(): (value: number) => string {
  const format = useFormatter();
  return (value) => format.number(value, { numberingSystem: "latn" });
}

export function codeOf(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** `length / max` in the page's digits. */
export function useCounter(): (length: number, max: number) => string {
  const number = useNumber();
  return (length, max) => `${number(length)} / ${number(max)}`;
}

/** A lead row's facts - score, owner, budget, source - each already words (LL-02). */
export function useLeadFacts(): (
  lead: LeadRead,
) => Array<{ key: string; label: string; value: ReactNode }> {
  const t = useTranslations("app.customers");
  const locale = useLocale();
  const number = useNumber();
  const { nameOf } = usePeople();
  return (lead) => {
    const budget = formatBudget(lead.budget_amount, lead.budget_currency, locale);
    return [
      { key: "score", label: t("facts.score"), value: <bdi>{number(lead.score)}</bdi> },
      {
        key: "owner",
        label: t("facts.owner"),
        value: <bdi>{nameOf(lead.assigned_to_id) ?? t("people.unassigned")}</bdi>,
      },
      ...(budget ? [{ key: "budget", label: t("facts.budget"), value: <bdi>{budget}</bdi> }] : []),
      {
        key: "source",
        label: t("facts.source"),
        value: t(`leadSource.${lead.source}` as "leadSource.manual"),
      },
    ];
  };
}
