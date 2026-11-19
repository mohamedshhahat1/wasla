"use client";

/**
 * APP-SET-02 at /settings/activity (AL-01 / AL-02, ADR-201): what happened in the workspace, newest
 * first, in words - who, what, on what, when in the workspace's time zone - with an action the
 * catalogue does not know shown as its code, as text. "Load more" follows the API's own paging
 * (`until`, exclusive); nothing of a row's details is rendered but the fields the screen can say.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { ActivityRow, Button, Card, EmptyState, ErrorState } from "@wasla/ui";

import { changesOf, hasWords, mergePages, nextUntil } from "../../lib/team/activity";
import { activityKey, workspaceKey } from "../../lib/team/keys";
import { isAdminRole } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useMembers } from "../inbox/people";
import { useSession } from "../session-provider";
import { busyProps, ScreenHead } from "../team/screen-head";
import { SettingsFrame } from "./settings-frame";

type Entry = components["schemas"]["AuditEntryRead"];

const HEADING = "activity-title";
/** One page: the API's default size, well under its 200 maximum. */
export const ACTIVITY_PAGE = 50;

export function ActivityLog() {
  const t = useTranslations("app.settings");
  const errors = useCodeText();
  const locale = useLocale();
  const { session, profile } = useSession();
  const { scope, role } = useWorkspace();
  const admin = isAdminRole(role);
  const members = useMembers();
  const workspace = useQuery({
    queryKey: workspaceKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace")),
    enabled: admin,
  });

  const pages = useInfiniteQuery({
    queryKey: activityKey(scope),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        session.api.GET("/api/v1/audit-logs", {
          params: {
            query: { limit: ACTIVITY_PAGE, ...(pageParam ? { until: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => (last.length >= ACTIVITY_PAGE ? nextUntil(last) : null),
    // A member never asks (APP-SET-02 is an owner's and an administrator's).
    enabled: admin,
  });

  if (!admin) {
    return (
      <SettingsFrame current="activity">
        <ScreenHead id={HEADING} title={t("activityTitle")} line={t("adminsOnly")} />
      </SettingsFrame>
    );
  }

  const zone = workspace.data?.timezone;
  const format = (iso: string, full: boolean) =>
    new Intl.DateTimeFormat(locale, {
      ...(zone ? { timeZone: zone } : {}),
      ...(full
        ? { dateStyle: "full" as const, timeStyle: "long" as const }
        : { dateStyle: "medium" as const, timeStyle: "short" as const }),
    }).format(new Date(iso));

  const actorOf = (entry: Entry): string => {
    if (entry.actor_kind === "system") return t("activity.system");
    if (entry.actor_kind === "platform_staff") return t("activity.platform");
    if (entry.actor_kind === "agent") return t("activity.agent");
    if (entry.actor_id && entry.actor_id === profile?.id) return t("activity.you");
    const known = entry.actor_id ? members.byId.get(entry.actor_id)?.name : undefined;
    return known ?? entry.actor_label ?? t("activity.former");
  };

  let body: React.ReactNode;
  if (pages.isError) {
    const error = pages.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("loadError")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void pages.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (pages.isPending) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else {
    const rows = mergePages(pages.data.pages);
    body = rows.length ? (
      <Card as="section" aria-labelledby="activity-list-title">
        <h2 id="activity-list-title" className="sr-only">
          {t("activityTitle")}
        </h2>
        <ul className="m-0 list-none p-0" data-activity-list="">
          {rows.map((entry) => {
            const worded = hasWords(entry.action);
            const changes = changesOf(entry.metadata);
            return (
              <ActivityRow
                key={entry.id}
                actor={actorOf(entry)}
                action={
                  worded
                    ? t(`activity.actions.${entry.action}` as "activity.actions.member_invited")
                    : entry.action
                }
                actionIsCode={!worded}
                target={entry.target_type === "workspace" ? null : (entry.target_label ?? null)}
                when={format(entry.occurred_at, false)}
                whenFull={format(entry.occurred_at, true)}
                change={
                  changes.length ? (
                    <>
                      {changes.map((change) => (
                        <span key={change.field} className="me-3 inline-block">
                          {t(`activity.fields.${change.field}`)}:{" "}
                          {change.from ? (
                            <>
                              <bdi>{change.from}</bdi> →{" "}
                            </>
                          ) : null}
                          <bdi>{change.to}</bdi>
                        </span>
                      ))}
                    </>
                  ) : null
                }
              />
            );
          })}
        </ul>
        {pages.hasNextPage ? (
          <div className="mt-4">
            <Button
              variant="secondary"
              {...busyProps(pages.isFetchingNextPage)}
              onClick={() => !pages.isFetchingNextPage && void pages.fetchNextPage()}
              data-action="load-more"
            >
              {t("activity.more")}
            </Button>
          </div>
        ) : (
          <p className="m-0 mt-4 text-sm text-text-muted">{t("activity.end")}</p>
        )}
      </Card>
    ) : (
      <EmptyState headingLevel={2} title={t("activity.empty")} />
    );
  }

  return (
    <SettingsFrame current="activity">
      <ScreenHead id={HEADING} title={t("activityTitle")} line={t("activityLine")} />
      {body}
    </SettingsFrame>
  );
}
