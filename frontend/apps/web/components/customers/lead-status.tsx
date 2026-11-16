"use client";

/**
 * A lead's status and its moves (LD-03, PX-03), for the lead page and the inbox panel alike. The
 * menu offers only `next_statuses` - the API's reading of the pipeline, never a copy of the graph
 * - and every move names the status the screen shows (`expected_status`). Nothing is optimistic:
 * a move a colleague made first (409 stale_lead_status) reloads the lead and says where it stands
 * now, read from the reload and never from the refusal; nothing is re-sent.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { StatusMenu, buttonClasses } from "@wasla/ui";

import {
  CONTACT_LEADS,
  LEAD_ACTIVITY,
  LEADS,
  LEAD_STATISTICS,
  resourcePrefix,
} from "../../lib/customers/keys";
import { statusMoveBody } from "../../lib/customers/lead-form";
import { leadHref, OPEN_LEAD_STATUSES } from "../../lib/customers/view";
import { leadKey } from "../../lib/inbox/keys";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { LeadStatusBadge, type LeadRead } from "./shared";

type Status = LeadRead["status"];

export type MoveNotice =
  | { kind: "stale"; status: Status }
  | { kind: "open-lead"; openId: string | null }
  | { kind: "error"; code: string | null };

export function useLeadStatusMove(lead: LeadRead) {
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [notice, setNotice] = useState<MoveNotice | null>(null);

  const reload = async (): Promise<LeadRead | null> => {
    try {
      const fresh = await unwrap(
        session.api.GET("/api/v1/leads/{lead_id}", { params: { path: { lead_id: lead.id } } }),
      );
      queryClient.setQueryData(leadKey(scope, lead.id), fresh);
      return fresh;
    } catch {
      return null;
    }
  };

  const move = useMutation({
    mutationFn: (to: Status) =>
      unwrap(
        session.api.POST("/api/v1/leads/{lead_id}/status", {
          params: { path: { lead_id: lead.id } },
          body: statusMoveBody(lead, to),
        }),
      ),
    onMutate: () => setNotice(null),
    // The answer replaces the cache - never a guess written before it (LD-09).
    onSuccess: (fresh) => {
      queryClient.setQueryData(leadKey(scope, lead.id), fresh);
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEADS) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEAD_STATISTICS) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEAD_ACTIVITY) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONTACT_LEADS) });
    },
    onError: async (error) => {
      const code = error instanceof ApiError ? error.code : null;
      if (code === "stale_lead_status" || code === "lead_status_transition_not_allowed") {
        const fresh = await reload();
        void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEAD_ACTIVITY) });
        setNotice(fresh ? { kind: "stale", status: fresh.status } : { kind: "error", code });
        return;
      }
      if (code === "open_lead_exists") {
        let openId: string | null = null;
        if (lead.contact_id) {
          try {
            const page = await unwrap(
              session.api.GET("/api/v1/leads", {
                params: {
                  query: {
                    contact_id: lead.contact_id,
                    status: [...OPEN_LEAD_STATUSES],
                    limit: 1,
                  },
                },
              }),
            );
            openId = page.items[0]?.id ?? null;
          } catch {
            openId = null;
          }
        }
        setNotice({ kind: "open-lead", openId });
        return;
      }
      setNotice({ kind: "error", code });
    },
  });

  return { move, notice, clearNotice: () => setNotice(null) };
}

/** The badge, the menu of allowed moves, and what a refused move says. */
export function LeadStatusControl({ lead }: { lead: LeadRead }) {
  const t = useTranslations("app.customers");
  const errors = useCodeText();
  const { move, notice } = useLeadStatusMove(lead);
  const closed =
    lead.next_statuses.length === 0
      ? lead.status === "won"
        ? t("status.wonClosed")
        : t("status.closed")
      : null;
  return (
    <span className="flex flex-col gap-2" data-lead-status={lead.status}>
      <StatusMenu
        current={<LeadStatusBadge status={lead.status} />}
        moves={lead.next_statuses.map((status) => ({
          value: status,
          label:
            lead.status === "lost" && status === "new"
              ? t("status.reopen")
              : t(`status.moveTo.${status}` as "status.moveTo.new"),
        }))}
        changeLabel={t("status.change")}
        closedLabel={closed}
        busy={move.isPending}
        onMove={(value) => {
          if (!move.isPending) move.mutate(value as Status);
        }}
      />
      {notice ? (
        <span
          role="status"
          className="flex flex-col items-start gap-2 text-sm text-text"
          data-move-notice={notice.kind}
        >
          {notice.kind === "stale"
            ? t("status.stale", {
                status: t(`leadStatus.${notice.status}` as "leadStatus.new"),
              })
            : notice.kind === "open-lead"
              ? errors("open_lead_exists")
              : errors(notice.code)}
          {notice.kind === "open-lead" && notice.openId ? (
            <NextLink href={leadHref(notice.openId)} className={buttonClasses("secondary")}>
              {t("create.openExisting")}
            </NextLink>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
