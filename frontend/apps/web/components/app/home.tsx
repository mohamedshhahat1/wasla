"use client";

import { useQuery } from "@tanstack/react-query";
import NextLink from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { ApiError, queryKey, unwrap } from "@wasla/api-client";
import { Button, Card, Meter, PageHeader, StatCard } from "@wasla/ui";

import { canManageChannels } from "../../lib/channels";
import { allowanceExhausted } from "../../lib/gates";
import { HOME_CONVERSATIONS } from "../../lib/stream-handlers";
import { useSession } from "../session-provider";
import { OnboardingChecklist } from "./onboarding-checklist";
import { useWorkspace } from "./workspace-context";

/** The page size home reads: one page, never a count endpoint (HM-01). */
export const HOME_PAGE_SIZE = 20;

/** A card's own loading, error and content; one card failing never blanks the page (HM-01). */
export function CardBody({
  query,
  children,
  framed = false,
}: {
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  children: () => ReactNode;
  /** A card of its own (the stat cards): loading and errors keep the card's frame. */
  framed?: boolean;
}) {
  const t = useTranslations("app");
  const frame = (content: ReactNode) =>
    framed ? (
      <div className="flex flex-col justify-center rounded-xl border border-border bg-surface p-5 shadow-sm">
        {content}
      </div>
    ) : (
      content
    );
  if (query.isPending) {
    return frame(
      <p role="status" className="m-0 text-sm text-text-muted">
        {t("home.loading")}
      </p>,
    );
  }
  if (query.isError) {
    const requestId = query.error instanceof ApiError ? query.error.requestId : null;
    return frame(
      <div className="flex flex-col items-start gap-2" data-card-error="">
        <p className="m-0 text-sm text-text">{t("home.cardError")}</p>
        {requestId ? (
          <p className="m-0 text-xs text-text-muted">
            {t("home.reference")} <bdi>{requestId}</bdi>
          </p>
        ) : null}
        <Button variant="secondary" onClick={() => void query.refetch()}>
          {t("home.retry")}
        </Button>
      </div>,
    );
  }
  return <>{children()}</>;
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="m-0 text-lg font-semibold text-text">{children}</h2>;
}

/** APP-HOME (HM-01, HM-02). */
export function Home() {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const format = useFormatter();
  const { session, profile } = useSession();
  const { scope, role, workspaceName, allowance } = useWorkspace();
  const manager = canManageChannels(role);

  const conversations = useQuery({
    queryKey: queryKey(scope, HOME_CONVERSATIONS),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/conversations", { params: { query: { limit: HOME_PAGE_SIZE } } }),
      ),
  });
  const connections = useQuery({
    queryKey: queryKey(scope, "channel-connections"),
    queryFn: () => unwrap(session.api.GET("/api/v1/channel-connections")),
  });
  const leads = useQuery({
    queryKey: queryKey(scope, "lead-statistics"),
    queryFn: () => unwrap(session.api.GET("/api/v1/leads/statistics")),
    enabled: manager,
  });
  const usage = useQuery({
    queryKey: queryKey(scope, "usage"),
    queryFn: () => unwrap(session.api.GET("/api/v1/usage")),
    enabled: manager,
  });

  const number = (value: number) => format.number(value, { numberingSystem: "latn" });
  const signedInAs = profile ? (
    <p className="m-0 text-sm text-text-muted" data-testid="signed-in-as">
      {auth.rich("home.signedInAs", {
        name: () => <bdi>{profile.full_name || profile.email}</bdi>,
        email: () => <bdi>{profile.email}</bdi>,
        workspace: () => <bdi data-testid="active-workspace">{workspaceName}</bdi>,
        role: () => <span data-testid="active-role">{role ? auth(`roles.${role}`) : ""}</span>,
      })}
    </p>
  ) : null;

  return (
    <div className="flex flex-col gap-6" data-testid="signed-in" data-page="home">
      <div className="flex flex-col gap-3 rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader
          title={t("home.title")}
          description={t.rich("home.line", { workspace: () => <bdi>{workspaceName}</bdi> })}
        />
        {signedInAs}
      </div>

      {manager ? <OnboardingChecklist /> : null}

      <div className="flex flex-col gap-4">
        <h2 id="home-today" className="sr-only">
          {t("home.title")}
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <CardBody query={conversations} framed>
            {() => {
              const page = conversations.data!;
              const count = page.items.length;
              const more = Boolean(page.next_cursor);
              return count === 0 ? (
                <Card as="div" data-testid="home-conversations-empty">
                  <p className="m-0 text-sm text-text-muted">{t("home.conversations")}</p>
                  <p className="m-0 mt-2 text-md text-text">{t("home.conversationsNone")}</p>
                </Card>
              ) : (
                <StatCard
                  testId="home-conversations"
                  label={t("home.conversations")}
                  value={
                    more ? t("home.conversationsMore", { count: number(count) }) : number(count)
                  }
                />
              );
            }}
          </CardBody>
          <CardBody query={connections} framed>
            {() => {
              const active = connections.data!.filter((row) => row.status === "active").length;
              return (
                <StatCard
                  testId="home-channels"
                  label={t("home.channels")}
                  value={number(active)}
                  sub={
                    <NextLink
                      href="/channels"
                      className="text-primary underline underline-offset-4"
                    >
                      {t("home.channelsLink")}
                    </NextLink>
                  }
                />
              );
            }}
          </CardBody>
        </div>
        {!manager && allowanceExhausted(allowance) ? (
          <p
            className="m-0 rounded-lg border border-border bg-info-subtle px-4 py-3 text-sm text-text"
            data-testid="home-ai-paused"
          >
            {t("gates.allowance.member")}
          </p>
        ) : null}
      </div>

      {manager ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card as="div" data-testid="home-leads">
            <div className="flex flex-col gap-4">
              <SectionTitle>
                <span id="home-leads">{t("home.leads")}</span>
              </SectionTitle>
              <CardBody query={leads}>
                {() => (
                  <dl className="m-0 grid grid-cols-3 gap-4">
                    {(
                      [
                        ["leadsOpen", leads.data!.open_leads],
                        ["leadsUnassigned", leads.data!.unassigned],
                        ["leadsTotal", leads.data!.total],
                      ] as const
                    ).map(([key, value]) => (
                      <div key={key} className="flex flex-col gap-1">
                        <dt className="text-sm text-text-muted">{t(`home.${key}`)}</dt>
                        <dd className="m-0 text-2xl font-semibold text-text">
                          <bdi>{number(value)}</bdi>
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </CardBody>
            </div>
          </Card>
          <Card as="div" data-testid="home-usage">
            <div className="flex flex-col gap-4">
              <SectionTitle>
                <span id="home-usage">{t("home.usage")}</span>
              </SectionTitle>
              <CardBody query={usage}>
                {() => {
                  const counters = usage.data!.counters;
                  const limit = allowance?.limit ?? null;
                  const used =
                    limit !== null && allowance
                      ? Math.max(0, limit - (allowance.remaining ?? 0))
                      : null;
                  return (
                    <div className="flex flex-col gap-4">
                      <dl className="m-0 grid grid-cols-2 gap-4">
                        <div className="flex flex-col gap-1">
                          <dt className="text-sm text-text-muted">{t("home.usageMessagesIn")}</dt>
                          <dd className="m-0 text-2xl font-semibold text-text">
                            <bdi>{number(counters.messages_received)}</bdi>
                          </dd>
                        </div>
                        <div className="flex flex-col gap-1">
                          <dt className="text-sm text-text-muted">{t("home.usageMessagesOut")}</dt>
                          <dd className="m-0 text-2xl font-semibold text-text">
                            <bdi>{number(counters.messages_sent)}</bdi>
                          </dd>
                        </div>
                      </dl>
                      {limit !== null && used !== null ? (
                        <Meter
                          label={t("home.aiTurns")}
                          value={used}
                          max={limit}
                          valueText={t("home.aiTurnsValue", {
                            used: number(used),
                            limit: number(limit),
                          })}
                          over={allowanceExhausted(allowance)}
                          overLabel={t("home.aiOver")}
                        />
                      ) : (
                        <p className="m-0 text-sm text-text-muted">
                          {t("home.aiTurnsUnlimited", { used: number(counters.ai_turns) })}
                        </p>
                      )}
                    </div>
                  );
                }}
              </CardBody>
            </div>
          </Card>
        </div>
      ) : null}
    </div>
  );
}
