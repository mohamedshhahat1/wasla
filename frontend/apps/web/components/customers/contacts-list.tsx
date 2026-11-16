"use client";

/**
 * APP-CUS-01, the list (CL-02): every customer who ever wrote to the workspace, on GET /contacts,
 * paged by its cursor and never past a null one. The search term lives in this component's state
 * only - never in the URL, the history or any storage - and is sent as typed: the server folds
 * Turkish letters, the browser never does (TF-03). The connected number is the one filter in the
 * URL, offered only when the workspace has more than one.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useState } from "react";

import { ApiError, queryKey, unwrap } from "@wasla/api-client";
import {
  Button,
  Card,
  ContactRow,
  EmptyState,
  ErrorState,
  FilterBar,
  Input,
  PageHeader,
  RecordList,
  Select,
} from "@wasla/ui";

import { CONTACTS } from "../../lib/customers/keys";
import {
  checkTerm,
  CONTACT_SEARCH_MAX,
  contactHref,
  contactsHref,
  parseContactsView,
  SEARCH_DEBOUNCE_MS,
} from "../../lib/customers/view";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { LeadStatusBadge, OptedOutBadge, PAGE_SIZE, rowLink, useNumber, useTimes } from "./shared";

const ALL = "all";

export function ContactsList() {
  const t = useTranslations("app.customers");
  const errors = useCodeText();
  const router = useRouter();
  const search = useSearchParams();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const times = useTimes();
  const number = useNumber();
  const searchId = useId();
  const view = useMemo(() => parseContactsView(new URLSearchParams(search.toString())), [search]);

  // CL-02: one read of the connections per workspace (FE-3's key, shared with the inbox); the
  // numbers' labels only when there is more than one to choose between.
  const connections = useQuery({
    queryKey: queryKey(scope, "channel-connections"),
    queryFn: () => unwrap(session.api.GET("/api/v1/channel-connections")),
    staleTime: 5 * 60_000,
  });
  const many = (connections.data?.length ?? 0) > 1;
  const accounts = useQuery({
    queryKey: queryKey(scope, "whatsapp-accounts"),
    queryFn: () => unwrap(session.api.GET("/api/v1/whatsapp/accounts")),
    staleTime: 5 * 60_000,
    enabled: many,
  });

  // The term: this component's state only (IL-05's rule), debounced, sent as typed.
  const [typed, setTyped] = useState("");
  const [term, setTerm] = useState<string | null>(null);
  const check = checkTerm(typed, CONTACT_SEARCH_MAX);
  const nextTerm = check.kind === "ok" ? check.term : null;
  useEffect(() => {
    const timer = setTimeout(() => setTerm(nextTerm), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [nextTerm]);

  const key = useMemo(
    () => [...queryKey(scope, CONTACTS, { account_id: view.accountId }), term ?? ""] as const,
    [scope, view.accountId, term],
  );
  const list = useInfiniteQuery({
    queryKey: key,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/contacts", {
          params: {
            query: {
              limit: PAGE_SIZE,
              ...(term ? { q: term } : {}),
              ...(view.accountId ? { account_id: view.accountId } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    refetchOnWindowFocus: true,
  });
  const rows = useMemo(() => list.data?.pages.flatMap((page) => page.items) ?? [], [list.data]);

  const accountOptions = [
    { value: ALL, label: t("contacts.allNumbers") },
    ...(accounts.data?.accounts ?? [])
      .filter((account) => account.display_phone_number)
      .map((account) => ({ value: account.id, label: account.display_phone_number ?? "" })),
  ];

  let body: React.ReactNode;
  if (list.isError) {
    const error = list.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("contacts.errorTitle")}
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
        {t("contacts.loading")}
      </p>
    );
  } else if (!rows.length) {
    body = term ? (
      <EmptyState
        headingLevel={2}
        title={t.rich("contacts.noMatch", { term: () => <bdi>{term}</bdi> })}
        action={
          <Button variant="secondary" onClick={() => setTyped("")}>
            {t("clearSearch")}
          </Button>
        }
      />
    ) : (
      <EmptyState
        headingLevel={2}
        title={t("contacts.emptyTitle")}
        description={t("contacts.emptyBody")}
      />
    );
  } else {
    body = (
      <Card as="div" data-contacts-list="">
        <RecordList
          label={t("contacts.listLabel")}
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
          {rows.map((contact) => {
            const name = contact.display_name;
            const seen = times(contact.last_inbound_at);
            const optedOut = Boolean(contact.opted_out_at);
            return (
              <ContactRow
                key={contact.id}
                href={contactHref(contact.id)}
                who={name || contact.wa_id || t("contacts.unnamed")}
                handle={name ? contact.wa_id : null}
                leadName={contact.lead?.name ?? null}
                leadStatus={contact.lead ? <LeadStatusBadge status={contact.lead.status} /> : null}
                consent={optedOut ? <OptedOutBadge /> : null}
                time={seen?.time ?? null}
                timeFull={seen?.full ?? null}
                renderLink={rowLink}
              />
            );
          })}
        </RecordList>
        <p role="status" aria-live="polite" className="sr-only">
          {t("contacts.shown", { count: rows.length, shown: number(rows.length) })}
        </p>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-6" data-screen="contacts">
      <PageHeader title={t("contacts.title")} description={t("contacts.description")} />
      <FilterBar label={t("filters")}>
        <div className="flex flex-col gap-1">
          <label htmlFor={searchId} className="text-sm font-medium text-text">
            {t("contacts.searchLabel")}
          </label>
          <div className="flex items-center gap-2">
            <Input
              id={searchId}
              type="search"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setTyped(event.target.value)}
              aria-describedby={check.kind === "too-long" ? `${searchId}-hint` : undefined}
              data-customers-search=""
            />
            {typed ? (
              <Button variant="ghost" onClick={() => setTyped("")}>
                {t("clearSearch")}
              </Button>
            ) : null}
          </div>
          {check.kind === "too-long" ? (
            <p id={`${searchId}-hint`} className="m-0 text-sm text-text-muted">
              {t("searchTooLong", { max: number(CONTACT_SEARCH_MAX) })}
            </p>
          ) : null}
        </div>
        {many ? (
          <div className="flex max-w-80 flex-col gap-1">
            <span id={`${searchId}-number`} className="text-sm font-medium text-text">
              {t("contacts.numberFilter")}
            </span>
            <Select
              aria-labelledby={`${searchId}-number`}
              value={view.accountId ?? ALL}
              options={accountOptions}
              onValueChange={(value) =>
                router.push(contactsHref({ accountId: value === ALL ? null : value }))
              }
            />
          </div>
        ) : null}
      </FilterBar>
      {body}
    </div>
  );
}
