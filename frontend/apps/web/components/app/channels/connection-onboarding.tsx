"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import NextLink from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { queryKey, unwrap } from "@wasla/api-client";
import { Badge, Banner, Button, buttonClasses, Card, Icon, KeyValueList, Link } from "@wasla/ui";

import { canManageChannels } from "../../../lib/channels";
import { disconnectReasonKey, META_PAYMENT_HELP } from "../../../lib/coexistence";
import { FormAlert } from "../../auth/form-alert";
import { useSession } from "../../session-provider";
import { useWorkspace } from "../workspace-context";
import { useOnboarding } from "./queries";

/**
 * The connection page's onboarding block (ADR-204, UD-01 .. UD-10): how the number was connected,
 * each step with its time, Coexistence's syncs, the payment-method step, the 13-day reminder and
 * Meta's disconnect reason - refreshed by the stream, never polled (UD-10).
 */
export function ConnectionOnboarding({ id }: { id: string }) {
  const t = useTranslations("app");
  const format = useFormatter();
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  const queryClient = useQueryClient();
  const onboarding = useOnboarding(id, true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const data = onboarding.data;
  if (!data) return null;

  const when = (value: string | null | undefined) =>
    value ? format.dateTime(new Date(value), { dateStyle: "medium", timeStyle: "short" }) : null;
  const embedded = data.method === "embedded_signup";
  const coexistence = data.mode === "coexistence";
  const disconnect = data.provider_disconnect ?? null;

  if (!embedded) {
    return (
      <p className="m-0 text-sm text-text-muted" data-testid="onboarding-method">
        {t("onboarding.assisted")}
      </p>
    );
  }

  const deadline = data.sync_deadline_at ? new Date(data.sync_deadline_at) : null;
  const syncsMissing = !data.contacts.requested_at || !data.history.requested_at;
  const deadlineOpen = deadline !== null && deadline.getTime() > Date.now();
  const history = data.history.declined_at
    ? t("onboarding.historyDeclined")
    : data.history.completed_at
      ? t("onboarding.historyComplete")
      : data.history.progress !== null && data.history.progress !== undefined
        ? t("onboarding.historyProgress", { progress: data.history.progress })
        : data.history.requested_at
          ? t("onboarding.historyRequested")
          : t("onboarding.notYet");
  const steps = [
    { key: "proved", label: t("onboarding.proved"), value: when(data.onboarded_at) ?? "—" },
    {
      key: "subscribed",
      label: t("onboarding.subscribed"),
      value: when(data.subscribed_at) ?? t("onboarding.notYet"),
    },
    {
      key: "registered",
      label: t("onboarding.registered"),
      value: data.registration_needed
        ? (when(data.registered_at) ?? t("onboarding.notYet"))
        : t("onboarding.notNeeded"),
    },
    ...(coexistence
      ? [
          {
            key: "contacts",
            label: t("onboarding.contacts"),
            value: when(data.contacts.requested_at) ?? t("onboarding.notYet"),
          },
          { key: "history", label: t("onboarding.history"), value: history },
        ]
      : []),
  ];
  const setupMissing = !data.subscribed_at || (data.registration_needed && !data.registered_at);

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/whatsapp/accounts/{account_id}/subscription", {
          params: { path: { account_id: id } },
        }),
      );
      await queryClient.invalidateQueries({
        queryKey: queryKey(scope, "whatsapp-onboarding", { id }),
      });
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card as="div" data-testid="onboarding">
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="m-0 text-lg font-semibold text-text">{t("onboarding.title")}</h2>
          <span data-testid="onboarding-method" className="inline-flex">
            <Badge tone="info">{t("onboarding.withMeta")}</Badge>
          </span>
          {coexistence ? (
            <span data-testid="onboarding-coexistence" className="inline-flex">
              <Badge tone="neutral">{t("onboarding.coexistence")}</Badge>
            </span>
          ) : null}
        </div>
        {disconnect ? (
          <Banner
            tone="danger"
            name="provider-disconnected"
            title={t("onboarding.disconnectedTitle")}
            action={
              canManageChannels(role) ? (
                <NextLink
                  href="/channels/connect/whatsapp-embedded"
                  className={buttonClasses("primary")}
                  data-testid="onboarding-reconnect"
                >
                  {t("onboarding.reconnect")}
                </NextLink>
              ) : undefined
            }
          >
            <span data-testid="onboarding-disconnect-reason">
              {t(`onboarding.reasons.${disconnectReasonKey(disconnect.reason)}`)}
            </span>
          </Banner>
        ) : null}
        <KeyValueList items={steps} />
        {coexistence && !disconnect && syncsMissing ? (
          <p className="m-0 text-sm text-text" role="status" data-testid="onboarding-deadline">
            {deadlineOpen
              ? t("onboarding.finishing", { deadline: when(data.sync_deadline_at) ?? "" })
              : t("onboarding.expired")}
          </p>
        ) : null}
        {coexistence && !disconnect && data.reminder_days ? (
          <p className="m-0 text-sm text-text" data-testid="onboarding-reminder">
            {t("onboarding.reminder", { days: data.reminder_days })}
          </p>
        ) : null}
        <div className="flex flex-col gap-1" data-testid="onboarding-payment">
          <p className="m-0 text-sm font-semibold text-text">{t("onboarding.paymentTitle")}</p>
          <p className="m-0 text-sm text-text">{t("onboarding.paymentBody")}</p>
          <Link
            href={META_PAYMENT_HELP}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-sm"
          >
            {t("onboarding.paymentLink")}
            <Icon icon={ExternalLink} name="ExternalLink" size={16} />
            <span className="sr-only"> {t("connect.guide.newTab")}</span>
          </Link>
        </div>
        {setupMissing && !disconnect && canManageChannels(role) ? (
          <div className="flex flex-col gap-2">
            <FormAlert error={error} />
            <Button onClick={() => void retry()} busy={busy} data-testid="onboarding-retry">
              {t("onboarding.retry")}
            </Button>
          </div>
        ) : null}
      </div>
    </Card>
  );
}
