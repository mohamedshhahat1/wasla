"use client";

import { useQuery } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { queryKey, unwrap } from "@wasla/api-client";
import { buttonClasses, Card, Checklist, type ChecklistItem } from "@wasla/ui";

import {
  CHECKLIST_STEPS,
  checklistLoading,
  checklistOpen,
  checklistStates,
  type ChecklistInputs,
  type ChecklistStep,
  type Read,
} from "../../lib/checklist";
import { READY_DOCUMENT } from "../../lib/knowledge/keys";
import { useAgents } from "../ai/agents-list";
import { useSession } from "../session-provider";
import { useWorkspace } from "./workspace-context";

function readOf<T>(query: { data: T | undefined; isError: boolean }): Read<T> {
  if (query.isError) return "error";
  return query.data ?? null;
}

/** The steps that have a screen get a link (FE-7 adds the team's, HM7-02); the others say they come later (SH-03). */
const STEP_LINKS: Partial<Record<ChecklistStep, { href: string; action: string }>> = {
  channel: { href: "/channels/connect/whatsapp", action: "channelAction" },
  agent: { href: "/ai/agents", action: "agentAction" },
  knowledge: { href: "/knowledge", action: "knowledgeAction" },
  team: { href: "/team/invitations", action: "teamAction" },
  // FE-8 (BL-04): the plan step leads to Billing.
  plan: { href: "/billing", action: "planAction" },
};

/**
 * APP-WS-03 (HM-03), for owners and admins: five reads made in parallel - each failing alone - and
 * one pure rule. Collapsing is for this session only (memory); nothing is stored; it disappears
 * once every step is done. No /billing/summary call: the plan step reads /workspace/gates.
 */
export function OnboardingChecklist() {
  const t = useTranslations("app");
  const { session } = useSession();
  const { scope, gates } = useWorkspace();
  const [collapsed, setCollapsed] = useState(false);

  const connections = useQuery({
    queryKey: queryKey(scope, "channel-connections"),
    queryFn: () => unwrap(session.api.GET("/api/v1/channel-connections")),
  });
  // The one agents read home, the checklist and the agents screen share (HM6-01).
  const agents = useAgents();
  // One ready document is enough to know the step is done (HM6-03): one row, never a count.
  const readyDocuments = useQuery({
    queryKey: queryKey(scope, READY_DOCUMENT, { limit: 1 }),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/knowledge/documents", {
          params: { query: { status: "ready", limit: 1 } },
        }),
      ),
  });
  const members = useQuery({
    queryKey: queryKey(scope, "members"),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace/members")),
  });

  const inputs: ChecklistInputs = {
    connections: readOf(connections),
    agents: readOf(agents),
    readyDocuments: (() => {
      const read = readOf(readyDocuments);
      return read === "error" || read === null ? read : read.items;
    })(),
    members: (() => {
      const read = readOf(members);
      return read === "error" || read === null ? read : read.members;
    })(),
    subscriptionStatus: gates ? (gates.subscription_status ?? "none") : null,
  };
  // Until every read has answered, no step claims a state ("could not check" is for a failure).
  if (checklistLoading(inputs)) {
    return (
      <Card as="div" data-testid="onboarding-checklist" data-loading="">
        <div className="flex flex-col gap-2">
          <h2 className="m-0 text-xl font-semibold text-text">{t("checklist.title")}</h2>
          <p role="status" className="m-0 text-sm text-text-muted">
            {t("home.loading")}
          </p>
        </div>
      </Card>
    );
  }
  const states = checklistStates(inputs);
  if (!checklistOpen(states)) return null;
  const done = CHECKLIST_STEPS.filter((step) => states[step] === "done").length;

  const items: ChecklistItem[] = CHECKLIST_STEPS.map((step) => {
    const link = STEP_LINKS[step];
    const state = states[step];
    return {
      key: step,
      label: t(`checklist.${step}`),
      state,
      ...(link
        ? state === "done"
          ? {}
          : {
              action: (
                <NextLink href={link.href} className={buttonClasses("secondary")}>
                  {t(`checklist.${link.action}` as "checklist.channelAction")}
                </NextLink>
              ),
            }
        : { detail: t("checklist.later") }),
    };
  });

  return (
    <Card as="div" data-testid="onboarding-checklist">
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h2 id="checklist-title" className="m-0 text-xl font-semibold text-text">
              {t("checklist.title")}
            </h2>
            <p className="m-0 text-sm text-text-muted">
              {t("checklist.line")}{" "}
              {t("checklist.progress", { done, total: CHECKLIST_STEPS.length })}
            </p>
          </div>
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-controls="checklist-steps"
            onClick={() => setCollapsed((value) => !value)}
            className={buttonClasses("ghost", "text-sm")}
          >
            {collapsed ? t("checklist.expand") : t("checklist.collapse")}
          </button>
        </div>
        <div id="checklist-steps" hidden={collapsed}>
          <Checklist
            items={items}
            stateLabels={{
              done: t("checklist.done"),
              undone: t("checklist.undone"),
              unknown: t("checklist.unknown"),
            }}
          />
        </div>
      </div>
    </Card>
  );
}
