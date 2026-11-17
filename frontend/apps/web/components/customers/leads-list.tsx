"use client";

/**
 * APP-CUS-02 (LL-01 .. LL-05): the leads on GET /leads with the server's filters and its cursor.
 * Status, source and assignee live in the URL; the search term and the tag are text a person
 * typed, so they live in this component's state only and are sent as typed (TF-03). The
 * statistics are an administrator's: for a member the query is never created, so the request is
 * never sent and no strip is drawn.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useState } from "react";

import { ApiError, queryKey, unwrap } from "@wasla/api-client";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  FilterBar,
  Input,
  LeadRow,
  PageHeader,
  RecordList,
  Select,
  StatCard,
} from "@wasla/ui";

import { LEADS, leadStatisticsKey } from "../../lib/customers/keys";
import {
  checkTerm,
  DEFAULT_LEADS_VIEW,
  isDefaultLeadsView,
  LEAD_SEARCH_MAX,
  LEAD_SOURCES,
  LEAD_STATUSES,
  LEAD_TAG_MAX,
  leadHref,
  leadsHref,
  leadsQuery,
  leadsViewKey,
  parseLeadsView,
  SEARCH_DEBOUNCE_MS,
  type LeadsView,
} from "../../lib/customers/view";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { LeadCreateDialog } from "./lead-create-dialog";
import {
  LeadStatusBadge,
  PAGE_SIZE,
  rowLink,
  useIsAdmin,
  useLeadFacts,
  useNumber,
  usePeople,
  useTimes,
} from "./shared";

const ANY = "any";

function useDebounced(value: string | null): string | null {
  const [settled, setSettled] = useState<string | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [value]);
  return settled;
}

export function LeadsList() {
  const t = useTranslations("app.customers");
  const errors = useCodeText();
  const router = useRouter();
  const search = useSearchParams();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const admin = useIsAdmin();
  const times = useTimes();
  const number = useNumber();
  const leadFacts = useLeadFacts();
  const { members, viewerId } = usePeople();
  const id = useId();
  const locale = useLocale();
  const [creating, setCreating] = useState(false);
  const view = useMemo(() => parseLeadsView(new URLSearchParams(search.toString())), [search]);
  const go = (next: LeadsView) => router.push(leadsHref(next));

  // Text a person typed: memory only (LY5-02), debounced, sent as typed.
  const [typed, setTyped] = useState("");
  const [typedTag, setTypedTag] = useState("");
  const termCheck = checkTerm(typed, LEAD_SEARCH_MAX);
  const tagCheck = checkTerm(typedTag, LEAD_TAG_MAX);
  const term = useDebounced(termCheck.kind === "ok" ? termCheck.term : null);
  const tag = useDebounced(tagCheck.kind === "ok" ? tagCheck.term : null);

  const key = useMemo(
    () => [...queryKey(scope, LEADS, leadsViewKey(view)), term ?? "", tag ?? ""] as const,
    [scope, view, term, tag],
  );
  const list = useInfiniteQuery({
    queryKey: key,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/leads", {
          params: {
            query: leadsQuery(view, {
              viewerId,
              search: term,
              tag,
              cursor: pageParam,
              limit: PAGE_SIZE,
            }),
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    refetchOnWindowFocus: true,
    // "Mine" needs the viewer's id before it can be asked.
    enabled: view.assignee !== "me" || Boolean(viewerId),
  });
  const rows = useMemo(() => list.data?.pages.flatMap((page) => page.items) ?? [], [list.data]);

  // LL-03: never created for a member.
  const statistics = useQuery({
    queryKey: leadStatisticsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/leads/statistics")),
    enabled: admin,
    refetchOnWindowFocus: true,
  });

  const memberOptions = useMemo(
    () =>
      [...members.byId.entries()]
        .filter(([memberId]) => memberId !== viewerId)
        .map(([memberId, member]) => ({ value: memberId, label: member.name }))
        .sort((a, b) => new Intl.Collator(locale).compare(a.label, b.label)),
    [members.byId, viewerId, locale],
  );
  const filtered = !isDefaultLeadsView(view) || Boolean(term) || Boolean(tag);
  const clearFilters = () => {
    setTyped("");
    setTypedTag("");
    go(DEFAULT_LEADS_VIEW);
  };

  let body: React.ReactNode;
  if (list.isError) {
    const error = list.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("leads.errorTitle")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void list.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (list.isPending) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("leads.loading")}
      </p>
    );
  } else if (!rows.length) {
    body = filtered ? (
      <EmptyState
        headingLevel={2}
        title={t("leads.noMatch")}
        action={
          <Button variant="secondary" onClick={clearFilters} data-clear-filters="">
            {t("leads.clearFilters")}
          </Button>
        }
      />
    ) : (
      <EmptyState
        headingLevel={2}
        title={t("leads.emptyTitle")}
        description={t("leads.emptyBody")}
      />
    );
  } else {
    body = (
      <Card as="div" data-leads-list="">
        <RecordList
          label={t("leads.listLabel")}
          busy={list.isFetchingNextPage}
          footer={
            list.hasNextPage ? (
              <Button
                variant="secondary"
                busy={list.isFetchingNextPage}
                onClick={() => void list.fetchNextPage()}
              >
                {t("loadMore")}
              </Button>
            ) : null
          }
        >
          {rows.map((lead) => {
            const at = times(lead.last_activity_at ?? lead.updated_at);
            return (
              <LeadRow
                key={lead.id}
                href={leadHref(lead.id)}
                who={lead.name || t("leads.unnamed")}
                status={<LeadStatusBadge status={lead.status} />}
                interest={lead.interest}
                facts={leadFacts(lead)}
                time={at?.time ?? null}
                timeFull={at?.full ?? null}
                renderLink={rowLink}
              />
            );
          })}
        </RecordList>
        <p role="status" aria-live="polite" className="sr-only">
          {t("leads.shown", { count: rows.length, shown: number(rows.length) })}
        </p>
      </Card>
    );
  }

  const stats = statistics.data;
  return (
    <div className="flex flex-col gap-6" data-screen="leads">
      <PageHeader
        title={t("leads.title")}
        description={t("leads.description")}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)} data-new-lead="">
            {t("create.open")}
          </Button>
        }
      />
      {admin && stats ? (
        <div
          role="group"
          aria-label={t("stats.label")}
          className="grid grid-cols-2 gap-4 lg:grid-cols-4"
          data-lead-statistics=""
        >
          <StatCard label={t("stats.total")} value={number(stats.total)} testId="stat-total" />
          <StatCard label={t("stats.open")} value={number(stats.open_leads)} testId="stat-open" />
          <StatCard
            label={t("stats.unassigned")}
            value={number(stats.unassigned)}
            testId="stat-unassigned"
          />
          <StatCard
            label={t("stats.won")}
            value={number(Number((stats.by_status as Record<string, number>).won ?? 0))}
            testId="stat-won"
          />
        </div>
      ) : null}
      <FilterBar label={t("filters")}>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-search`} className="text-sm font-medium text-text">
              {t("leads.searchLabel")}
            </label>
            <Input
              id={`${id}-search`}
              type="search"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setTyped(event.target.value)}
              aria-describedby={termCheck.kind === "too-long" ? `${id}-search-hint` : undefined}
              data-customers-search=""
            />
            {termCheck.kind === "too-long" ? (
              <p id={`${id}-search-hint`} className="m-0 text-sm text-text-muted">
                {t("searchTooLong", { max: number(LEAD_SEARCH_MAX) })}
              </p>
            ) : null}
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-tag`} className="text-sm font-medium text-text">
              {t("leads.tagLabel")}
            </label>
            <Input
              id={`${id}-tag`}
              value={typedTag}
              autoComplete="off"
              spellCheck={false}
              dir="auto"
              onChange={(event) => setTypedTag(event.target.value)}
              aria-describedby={tagCheck.kind === "too-long" ? `${id}-tag-hint` : undefined}
              data-leads-tag=""
            />
            {tagCheck.kind === "too-long" ? (
              <p id={`${id}-tag-hint`} className="m-0 text-sm text-text-muted">
                {t("searchTooLong", { max: number(LEAD_TAG_MAX) })}
              </p>
            ) : null}
          </div>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <span id={`${id}-status`} className="text-sm font-medium text-text">
              {t("leads.statusFilter")}
            </span>
            <Select
              aria-labelledby={`${id}-status`}
              value={view.status ?? ANY}
              options={[
                { value: ANY, label: t("leads.anyStatus") },
                ...LEAD_STATUSES.map((status) => ({
                  value: status,
                  label: t(`leadStatus.${status}`),
                })),
              ]}
              onValueChange={(value) =>
                go({ ...view, status: value === ANY ? null : (value as LeadsView["status"]) })
              }
              data-filter="status"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span id={`${id}-source`} className="text-sm font-medium text-text">
              {t("leads.sourceFilter")}
            </span>
            <Select
              aria-labelledby={`${id}-source`}
              value={view.source ?? ANY}
              options={[
                { value: ANY, label: t("leads.anySource") },
                ...LEAD_SOURCES.map((source) => ({
                  value: source,
                  label: t(`leadSource.${source}`),
                })),
              ]}
              onValueChange={(value) =>
                go({ ...view, source: value === ANY ? null : (value as LeadsView["source"]) })
              }
              data-filter="source"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span id={`${id}-assignee`} className="text-sm font-medium text-text">
              {t("leads.assigneeFilter")}
            </span>
            <Select
              aria-labelledby={`${id}-assignee`}
              value={view.assignee ?? ANY}
              options={[
                { value: ANY, label: t("leads.anyone") },
                { value: "me", label: t("leads.mine") },
                { value: "none", label: t("people.unassigned") },
                ...memberOptions,
              ]}
              onValueChange={(value) => go({ ...view, assignee: value === ANY ? null : value })}
            />
          </div>
        </div>
        {filtered ? (
          <div className="flex justify-start">
            <Button variant="ghost" onClick={clearFilters}>
              {t("leads.clearFilters")}
            </Button>
          </div>
        ) : null}
      </FilterBar>
      {body}
      {creating ? <LeadCreateDialog open onOpenChange={setCreating} /> : null}
    </div>
  );
}
