"use client";

import { ChevronRight, MessageCircle } from "lucide-react";
import NextLink from "next/link";
import { useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import type { components } from "@wasla/api-client";
import {
  Badge,
  Button,
  buttonClasses,
  Card,
  EmptyState,
  ErrorState,
  Icon,
  Meter,
  PageHeader,
  type StatusTone,
} from "@wasla/ui";

import {
  canChooseConnections,
  canManageChannels,
  connectDecision,
  type ConnectRefusal,
} from "../../../lib/channels";
import { useWorkspace } from "../workspace-context";
import { useChannelReads } from "./queries";
import { SelectionDialog } from "./selection-dialog";

type Connection = components["schemas"]["ChannelConnectionRead"];
type Account = components["schemas"]["WhatsAppAccountResponse"];

const STATUS_TONE: Record<string, StatusTone> = {
  active: "success",
  disabled: "warning",
  released: "neutral",
};

/** The Connect action, offered only as the connect rule allows, with its reason as visible text. */
export function ConnectAction({
  refusal,
  allowed,
}: {
  refusal: ConnectRefusal | null;
  allowed: boolean;
}) {
  const t = useTranslations("app");
  if (refusal === "role") {
    return (
      <p className="m-0 text-sm text-text-muted" data-testid="connect-role-note">
        {t("roles.onlyAdmins")}
      </p>
    );
  }
  if (allowed) {
    return (
      <NextLink
        href="/channels/connect/whatsapp"
        className={buttonClasses("primary")}
        data-testid="connect-action"
      >
        {t("channels.connectWhatsApp")}
      </NextLink>
    );
  }
  const reason =
    refusal === "noSlots"
      ? t("channels.noSlots")
      : refusal === "notAllowed"
        ? t("channels.notAllowed")
        : null;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button
        disabled
        aria-describedby={reason ? "connect-reason" : undefined}
        data-testid="connect-action"
      >
        {t("channels.connectWhatsApp")}
      </Button>
      {reason ? (
        <p id="connect-reason" className="m-0 text-sm text-text" data-testid="connect-reason">
          {reason}
        </p>
      ) : null}
    </div>
  );
}

function ConnectionRow({
  connection,
  account,
}: {
  connection: Connection;
  account: Account | undefined;
}) {
  const t = useTranslations("app");
  const format = useFormatter();
  const number = account?.display_phone_number ?? connection.external_account_id;
  const name = account?.display_name ?? account?.verified_name ?? null;
  const disabledAt = connection.disabled_at
    ? format.dateTime(new Date(connection.disabled_at), { dateStyle: "medium" })
    : "";
  return (
    <li className="m-0" data-connection={connection.id} data-status={connection.status}>
      <NextLink
        href={`/channels/${connection.id}`}
        className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-surface px-4 py-4 no-underline shadow-sm hover:border-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        <span className="inline-flex size-10 shrink-0 items-center justify-center rounded-full bg-secondary-subtle text-primary">
          <Icon icon={MessageCircle} name="MessageCircle" />
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-md font-semibold text-text">
            <bdi dir="ltr">{number}</bdi>
            {name ? (
              <span className="font-normal text-text-muted">
                {" · "}
                <bdi>{name}</bdi>
              </span>
            ) : null}
          </span>
          <span className="text-sm text-text-muted">
            {t(`channels.${connection.channel}`)}
            {" · "}
            {t(`channels.health.${connection.health}`)}
            {connection.status === "disabled" && connection.disabled_reason
              ? ` · ${
                  connection.disabled_reason === "manual"
                    ? t("channels.disabledManually", { date: disabledAt })
                    : t("channels.disabledByPlan", { date: disabledAt })
                }`
              : null}
            {connection.counts_toward_capacity ? null : ` · ${t("channels.notCounted")}`}
          </span>
        </span>
        <Badge tone={STATUS_TONE[connection.status] ?? "neutral"}>
          {t(`channels.status.${connection.status}`)}
        </Badge>
        <Icon icon={ChevronRight} name="ChevronRight" className="text-text-muted" />
      </NextLink>
    </li>
  );
}

/** APP-CH-01 (CN-05): every member reads it; owners and admins act; no polling. */
export function ChannelsList() {
  const t = useTranslations("app");
  const format = useFormatter();
  const { role } = useWorkspace();
  const { connections, capacity, accounts } = useChannelReads();
  const [showReleased, setShowReleased] = useState(false);
  const [choosing, setChoosing] = useState(false);
  const released_ok = useSearchParams().get("notice") === "released";

  const cap = capacity.data ?? null;
  const decision = connectDecision({
    role,
    channel: "whatsapp",
    capacity: cap
      ? { remaining: cap.remaining, allowed_channel_types: cap.breakdown.allowed_channel_types }
      : null,
  });
  const action = (
    <ConnectAction allowed={decision.allowed} refusal={decision.allowed ? null : decision.reason} />
  );
  const byId = new Map((accounts.data?.accounts ?? []).map((account) => [account.id, account]));
  const released = (accounts.data?.accounts ?? []).filter(
    (account) => account.status === "released",
  );
  const number = (value: number) => format.number(value, { numberingSystem: "latn" });
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "long" });

  return (
    <div className="flex flex-col gap-6" data-page="channels">
      <div className="rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader
          title={t("channels.title")}
          description={t("channels.line")}
          actions={canManageChannels(role) && connections.data?.length ? action : undefined}
        />
      </div>

      {released_ok ? (
        <p
          role="status"
          className="m-0 rounded-lg border border-border bg-surface px-4 py-3 text-sm text-text"
          data-testid="released-notice"
        >
          {t("detail.released_ok")}
        </p>
      ) : null}

      {cap ? (
        <Card as="div" data-testid="capacity">
          <div className="flex flex-col gap-4">
            <h2 id="capacity-title" className="m-0 text-lg font-semibold text-text">
              {t("channels.capacity")}
            </h2>
            {cap.effective_limit !== null ? (
              <Meter
                label={t("channels.slotsInUse")}
                value={cap.active}
                max={cap.effective_limit}
                valueText={t("channels.capacityValue", {
                  active: number(cap.active),
                  limit: number(cap.effective_limit),
                })}
                over={cap.over_limit}
                overLabel={t("channels.capacityOver")}
              />
            ) : (
              <p className="m-0 text-md text-text">
                {t("channels.capacityUnlimited", { active: number(cap.active) })}
              </p>
            )}
            {cap.scheduled && cap.scheduled.effective_limit !== null ? (
              <p className="m-0 text-sm text-text-muted" data-testid="capacity-scheduled">
                {t("channels.scheduled", {
                  date: date(cap.scheduled.effective_at),
                  limit: number(cap.scheduled.effective_limit),
                })}
              </p>
            ) : null}
          </div>
        </Card>
      ) : capacity.isError ? (
        <ErrorState title={t("channels.loadError")} />
      ) : null}

      {cap?.reduction && !cap.reduction.resolved_at ? (
        <Card as="div" data-testid="reduction-notice">
          <div className="flex flex-col gap-3">
            <h2 id="reduction-title" className="m-0 text-lg font-semibold text-text">
              {t("channels.reductionTitle")}
            </h2>
            <p className="m-0 text-md text-text">
              {t("channels.reductionBody", {
                date: date(cap.reduction.effective_at),
                limit: cap.reduction.target_general_limit,
              })}
            </p>
            {canChooseConnections(role) ? (
              <div>
                <Button
                  variant="secondary"
                  onClick={() => setChoosing(true)}
                  data-testid="choose-connections"
                >
                  {t("channels.choose")}
                </Button>
              </div>
            ) : (
              <p className="m-0 text-sm text-text-muted">{t("roles.onlyOwner")}</p>
            )}
          </div>
        </Card>
      ) : null}

      {connections.isError ? (
        <ErrorState title={t("channels.loadError")} />
      ) : connections.data && connections.data.length === 0 ? (
        <EmptyState
          title={t("channels.emptyTitle")}
          description={t("channels.emptyBody")}
          action={
            canManageChannels(role) ? (
              action
            ) : (
              <p className="m-0 text-sm text-text-muted">{t("roles.onlyAdmins")}</p>
            )
          }
        />
      ) : connections.data ? (
        <div>
          <ul className="m-0 flex list-none flex-col gap-3 p-0" data-testid="connections">
            {connections.data.map((connection) => (
              <ConnectionRow
                key={connection.id}
                connection={connection}
                account={byId.get(connection.id)}
              />
            ))}
          </ul>
          {!canManageChannels(role) ? (
            <p className="m-0 mt-4 text-sm text-text-muted">{t("roles.onlyAdmins")}</p>
          ) : null}
        </div>
      ) : (
        <p role="status" className="m-0 text-sm text-text-muted">
          {t("home.loading")}
        </p>
      )}

      {released.length ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="released-title" className="m-0 text-lg font-semibold text-text">
              {t("channels.released")}
            </h2>
            <button
              type="button"
              aria-expanded={showReleased}
              aria-controls="released-list"
              className={buttonClasses("ghost", "text-sm")}
              onClick={() => setShowReleased((value) => !value)}
            >
              {showReleased ? t("channels.hideReleased") : t("channels.showReleased")}
            </button>
          </div>
          <div id="released-list" hidden={!showReleased}>
            <p className="m-0 mb-3 text-sm text-text-muted">{t("channels.releasedHint")}</p>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {released.map((account) => (
                <li
                  key={account.id}
                  className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface px-4 py-3"
                >
                  <bdi dir="ltr" className="text-md text-text">
                    {account.display_phone_number}
                  </bdi>
                  <Badge tone="neutral">{t("channels.status.released")}</Badge>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {choosing && cap && connections.data ? (
        <SelectionDialog
          open={choosing}
          onOpenChange={setChoosing}
          capacity={cap}
          connections={connections.data.filter((row) => row.status === "active")}
          accounts={byId}
        />
      ) : null}
    </div>
  );
}
