"use client";

import { useQueries } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { queryKey, unwrap } from "@wasla/api-client";
import { Banner, buttonClasses } from "@wasla/ui";

import { tabFlags } from "../../lib/account/tab-flags";
import { coexistenceWarnings } from "../../lib/coexistence";
import { useSession } from "../session-provider";
import { useChannelReads } from "./channels/queries";
import { useWorkspace } from "./workspace-context";

const DISMISSED = "wasla.coexistence-reminder.dismissed";
/** At most this many numbers are asked about; a workspace with more sees the first ones. */
const MAX_NUMBERS = 10;

/**
 * Home's two Coexistence warnings (ADR-204, HM9-01 .. HM9-04), after the existing ones and in
 * their order: the 13-day reminder for an active Coexistence number (owners and administrators,
 * put away for the session only) and "Number disconnected by Meta" with its page. No warning
 * claims a missing payment method: Meta does not say (HM9-03).
 */
export function CoexistenceWarnings({ manager }: { manager: boolean }) {
  const t = useTranslations("app.homeWarnings");
  const { session } = useSession();
  const { scope } = useWorkspace();
  const { accounts, connections } = useChannelReads();
  const [dismissed, setDismissed] = useState(() => tabFlags()?.getItem(DISMISSED) === "1");
  const live = (accounts.data?.accounts ?? [])
    .filter((account) => account.status !== "released")
    .slice(0, MAX_NUMBERS);
  const onboarding = useQueries({
    queries: live.map((account) => ({
      queryKey: queryKey(scope, "whatsapp-onboarding", { id: account.id }),
      queryFn: () =>
        unwrap(
          session.api.GET("/api/v1/whatsapp/accounts/{account_id}/onboarding", {
            params: { path: { account_id: account.id } },
          }),
        ),
      retry: false,
    })),
  });
  const warnings = coexistenceWarnings(
    live.map((account, index) => ({
      id: account.id,
      status: account.status,
      mode: onboarding[index]?.data?.mode ?? null,
      reminderDays: onboarding[index]?.data?.reminder_days ?? null,
      health: connections.data?.find((row) => row.id === account.id)?.health ?? null,
    })),
  );
  if (!warnings.reminder && !warnings.disconnected) return null;
  return (
    <div className="flex flex-col gap-3" data-coexistence-warnings="">
      {warnings.disconnected ? (
        <Banner
          tone="danger"
          name="number-disconnected"
          title={t("disconnectedTitle")}
          action={
            <NextLink
              href={`/channels/${warnings.disconnected}`}
              className={buttonClasses("secondary")}
            >
              {t("disconnectedAction")}
            </NextLink>
          }
        />
      ) : null}
      {warnings.reminder && manager && !dismissed ? (
        <Banner
          tone="info"
          name="coexistence-reminder"
          title={t("reminderTitle", { days: warnings.reminder.days })}
          onDismiss={() => {
            tabFlags()?.setItem(DISMISSED, "1");
            setDismissed(true);
          }}
          dismissLabel={t("reminderDismiss")}
        >
          {t("reminderBody")}
        </Banner>
      ) : null}
    </div>
  );
}
