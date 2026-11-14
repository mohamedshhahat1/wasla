"use client";

/**
 * APP-INBOX-03 (PN-01): who the customer is - from the conversation's participant - and their lead
 * (GET /leads/{id}) when there is one. Read only: the contact detail, its history and lead editing
 * are FE-5's (FEB-19, APP-CUS-03), so no link is drawn to a screen that does not exist.
 */
import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { IconButton, KeyValueList } from "@wasla/ui";

import { leadKey } from "../../lib/inbox/keys";
import { fullTime } from "../../lib/inbox/time";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { useMembers, useNumbers, whoOf, type ConversationRead } from "./people";

export function CustomerPanelBody({ conversation }: { conversation: ConversationRead }) {
  const t = useTranslations("app.inbox.panel");
  const inbox = useTranslations("app.inbox");
  const locale = useLocale();
  const format = useFormatter();
  const { session, profile } = useSession();
  const { scope } = useWorkspace();
  const members = useMembers();
  const numbers = useNumbers();
  const [copied, setCopied] = useState(false);
  const leadId = conversation.participant.lead_id ?? null;
  const lead = useQuery({
    queryKey: leadKey(scope, leadId ?? "none"),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/leads/{lead_id}", { params: { path: { lead_id: leadId! } } }),
      ),
    enabled: Boolean(leadId),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });

  const nameOf = (id: string | null | undefined) =>
    id
      ? id === profile?.id
        ? inbox("assignee.you")
        : (members.byId.get(id)?.name ?? inbox("assignee.someone"))
      : inbox("assignee.none");
  const handle = conversation.participant.handle;
  const number = numbers.numberOf(conversation.connection_id);

  return (
    <>
      <div className="flex flex-col gap-3">
        <h3 className="m-0 text-md font-semibold text-text">{t("customer")}</h3>
        <KeyValueList
          items={[
            {
              key: "name",
              label: t("name"),
              value: <bdi>{whoOf(conversation) ?? inbox("unknownCustomer")}</bdi>,
            },
            ...(handle
              ? [
                  {
                    key: "handle",
                    label: t("number"),
                    value: (
                      <span className="inline-flex items-center gap-1">
                        <bdi dir="ltr">{handle}</bdi>
                        <IconButton
                          icon={Copy}
                          iconName="Copy"
                          label={copied ? t("copied") : t("copy")}
                          data-copy-number=""
                          onClick={() => {
                            void navigator.clipboard?.writeText(handle).then(
                              () => setCopied(true),
                              () => undefined,
                            );
                          }}
                        />
                      </span>
                    ),
                  },
                ]
              : []),
            {
              key: "channel",
              label: t("channel"),
              value: inbox(`channel.${conversation.channel}`),
            },
            ...(number
              ? [{ key: "line", label: t("line"), value: <bdi dir="ltr">{number}</bdi> }]
              : []),
          ]}
        />
        {copied ? (
          <p role="status" className="m-0 text-xs text-text-muted">
            {t("copied")}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-3">
        <h3 className="m-0 text-md font-semibold text-text">{t("conversation")}</h3>
        <KeyValueList
          items={[
            { key: "status", label: t("status"), value: inbox(`status.${conversation.status}`) },
            { key: "mode", label: t("mode"), value: inbox(`mode.${conversation.mode}`) },
            {
              key: "assignee",
              label: t("assignee"),
              value: <bdi>{nameOf(conversation.assigned_to_id)}</bdi>,
            },
            {
              key: "priority",
              label: t("priority"),
              value: inbox(`priority.${conversation.priority}`),
            },
            {
              key: "created",
              label: t("started"),
              value: fullTime(conversation.created_at, locale),
            },
            ...(conversation.last_message_at
              ? [
                  {
                    key: "last",
                    label: t("lastActivity"),
                    value: fullTime(conversation.last_message_at, locale),
                  },
                ]
              : []),
          ]}
        />
      </div>

      <div className="flex flex-col gap-3" data-panel-lead="">
        <h3 className="m-0 text-md font-semibold text-text">{t("lead")}</h3>
        {!leadId ||
        (lead.isError && lead.error instanceof ApiError && lead.error.status === 404) ? (
          <p className="m-0 text-sm text-text-muted">{t("noLead")}</p>
        ) : lead.isPending ? (
          <p role="status" className="m-0 text-sm text-text-muted">
            {t("loading")}
          </p>
        ) : lead.isError ? (
          <p className="m-0 text-sm text-text">{t("leadError")}</p>
        ) : (
          <KeyValueList
            items={[
              {
                key: "status",
                label: t("leadStatus"),
                value: t(`leadStatusValue.${lead.data.status}`),
              },
              {
                key: "score",
                label: t("score"),
                value: <bdi>{format.number(lead.data.score, { numberingSystem: "latn" })}</bdi>,
              },
              { key: "source", label: t("source"), value: <bdi>{lead.data.source}</bdi> },
              {
                key: "owner",
                label: t("leadOwner"),
                value: <bdi>{nameOf(lead.data.assigned_to_id)}</bdi>,
              },
              ...(lead.data.interest
                ? [
                    {
                      key: "interest",
                      label: t("interest"),
                      value: <bdi dir="auto">{lead.data.interest}</bdi>,
                    },
                  ]
                : []),
              ...(lead.data.tags.length
                ? [
                    {
                      key: "tags",
                      label: t("tags"),
                      value: <bdi dir="auto">{lead.data.tags.join(", ")}</bdi>,
                    },
                  ]
                : []),
              ...(lead.data.last_activity_at
                ? [
                    {
                      key: "activity",
                      label: t("leadActivity"),
                      value: fullTime(lead.data.last_activity_at, locale),
                    },
                  ]
                : []),
            ]}
          />
        )}
      </div>
    </>
  );
}
