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
import { useSession } from "../session-provider";
import { useWorkspace } from "./workspace-context";

function readOf<T>(query: { data: T | undefined; isError: boolean }): Read<T> {
  if (query.isError) return "error";
  return query.data ?? null;
}

/** The steps that have a screen in FE-3 get a link; the others say they come later (SH-03). */
const STEP_LINKS: Partial<Record<ChecklistStep, string>> = {
  channel: "/channels/connect/whatsapp",
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
  const agents = useQuery({
    queryKey: queryKey(scope, "agents"),
    queryFn: () => unwrap(session.api.GET("/api/v1/agents")),
  });
  const bases = useQuery({
    queryKey: queryKey(scope, "knowledge-bases"),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/knowledge/bases", { params: { query: { limit: 1 } } })),
  });
  const members = useQuery({
    queryKey: queryKey(scope, "members"),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace/members")),
  });

  const inputs: ChecklistInputs = {
    connections: readOf(connections),
    agents: readOf(agents),
    bases: readOf(bases),
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
    const href = STEP_LINKS[step];
    const state = states[step];
    return {
      key: step,
      label: t(`checklist.${step}`),
      state,
      ...(href
        ? state === "done"
          ? {}
          : {
              action: (
                <NextLink href={href} className={buttonClasses("secondary")}>
                  {t("checklist.channelAction")}
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
