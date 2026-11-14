"use client";

/**
 * APP-INBOX-01 (IL-01 .. IL-07): the list with the server's views, search held in memory only,
 * paged by the view-bound cursor, rows with their preview and the team's unread count, and the
 * live order that never moves under the person's pointer.
 */
import { useInfiniteQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { MessageCircle, MoreVertical } from "lucide-react";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { ApiError, queryKey, unwrap, type components } from "@wasla/api-client";
import {
  Badge,
  Button,
  ConversationList,
  ConversationRow,
  DropdownMenu,
  EmptyState,
  ErrorState,
  IconButton,
  Input,
  SegmentedControl,
  Select,
} from "@wasla/ui";

import { INBOX } from "../../lib/inbox/keys";
import { applyListUpdateNow, capCount } from "../../lib/inbox/live";
import { fullTime, relativeTime } from "../../lib/inbox/time";
import {
  checkSearch,
  inboxHref,
  listQuery,
  SEARCH_DEBOUNCE_MS,
  STATUSES,
  viewKey,
  type InboxView,
} from "../../lib/inbox/view";
import { appNow } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { useCodeText } from "./error-text";
import { useInboxRuntime } from "./inbox-runtime";
import { handleLine, useMembers, useNumbers, whoOf, type ConversationRead } from "./people";

type ListPage = components["schemas"]["ConversationListPage"];

export const PAGE_SIZE = 30;

/** One shared timer per list for the relative times: at most once a minute (IL-03). */
function useMinuteClock(): number {
  const [now, setNow] = useState(() => appNow());
  useEffect(() => {
    const timer = setInterval(() => setNow(appNow()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

export function listKey(
  scope: { workspaceId: string; sessionGeneration: number },
  view: InboxView,
) {
  // The search term is in the key in memory only (IL-05); the key is never persisted anywhere.
  return queryKey(scope, INBOX, viewKey(view));
}

export function ConversationListPane({
  view,
  openId,
  onMarkUnread,
}: {
  view: InboxView;
  openId: string | null;
  onMarkUnread: (id: string) => void;
}) {
  const t = useTranslations("app.inbox");
  const errors = useCodeText();
  const locale = useLocale();
  const format = useFormatter();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { session, profile } = useSession();
  const { scope } = useWorkspace();
  const runtime = useInboxRuntime();
  const members = useMembers();
  const numbers = useNumbers();
  const now = useMinuteClock();
  const searchId = useId();

  // IL-05: the term lives in this component's state only.
  const [typed, setTyped] = useState("");
  const [term, setTerm] = useState<string | null>(null);
  const check = checkSearch(typed);
  useEffect(() => {
    const next = check.kind === "ok" ? check.term : null;
    const timer = setTimeout(() => setTerm(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [check.kind, check.kind === "ok" ? check.term : null]); // eslint-disable-line react-hooks/exhaustive-deps

  const baseKey = listKey(scope, view);
  const key = useMemo(() => [...baseKey, term ?? ""] as const, [baseKey, term]);
  const mismatches = useRef(0);

  const list = useInfiniteQuery({
    queryKey: key,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      try {
        const page = await unwrap(
          session.api.GET("/api/v1/conversations", {
            params: { query: listQuery(view, term, pageParam, PAGE_SIZE) },
            signal,
          }),
        );
        mismatches.current = 0;
        return page;
      } catch (error) {
        // IL-04: a cursor of another view → drop the pages and read from the top, once.
        if (
          error instanceof ApiError &&
          error.code === "cursor_view_mismatch" &&
          pageParam &&
          mismatches.current === 0
        ) {
          mismatches.current += 1;
          queueMicrotask(() =>
            queryClient.setQueryData<InfiniteData<ListPage, string | null>>(key, (data) =>
              data ? { pages: data.pages.slice(0, 1), pageParams: [null] } : data,
            ),
          );
          return unwrap(
            session.api.GET("/api/v1/conversations", {
              params: { query: listQuery(view, term, null, PAGE_SIZE) },
              signal,
            }),
          );
        }
        throw error;
      }
    },
    getNextPageParam: (last) => last.next_cursor ?? null,
  });

  useEffect(() => {
    runtime.setPollAfter(list.data?.pages[0]?.poll_after_seconds ?? null);
  }, [list.data, runtime]);

  // IL-06: a refreshed first page applies at once only at the top with no row focused.
  const listRef = useRef<HTMLDivElement | null>(null);
  const [shown, setShown] = useState<ConversationRead[]>([]);
  const [waiting, setWaiting] = useState<ConversationRead[] | null>(null);
  const fetched = useMemo(() => list.data?.pages.flatMap((page) => page.items) ?? [], [list.data]);
  useEffect(() => {
    const box = listRef.current;
    const scrolledAway = Boolean(box && box.scrollTop > 0);
    const rowFocused = Boolean(
      box && box.contains(document.activeElement) && document.activeElement !== box,
    );
    const sameOrder =
      shown.length === fetched.length && shown.every((row, index) => row.id === fetched[index]?.id);
    if (sameOrder || shown.length === 0 || applyListUpdateNow({ scrolledAway, rowFocused })) {
      setShown(fetched);
      setWaiting(null);
    } else {
      setWaiting(fetched);
    }
  }, [fetched]); // eslint-disable-line react-hooks/exhaustive-deps

  // "Load more" also loads when it scrolls into view.
  const observer = useRef<IntersectionObserver | null>(null);
  const sentinelRef = useCallback(
    (node: HTMLElement | null) => {
      observer.current?.disconnect();
      if (!node || typeof IntersectionObserver === "undefined") return;
      observer.current = new IntersectionObserver((entries) => {
        if (
          entries.some((entry) => entry.isIntersecting) &&
          list.hasNextPage &&
          !list.isFetchingNextPage
        ) {
          void list.fetchNextPage();
        }
      });
      observer.current.observe(node);
    },
    [list],
  );

  const go = (next: InboxView) => router.push(inboxHref(next, openId));
  const number = (value: number) => format.number(value, { numberingSystem: "latn" });

  const memberOptions = [...members.byId.entries()]
    .sort(([a], [b]) => (a === profile?.id ? -1 : b === profile?.id ? 1 : 0))
    .map(([id, member]) => ({
      value: id,
      label: id === profile?.id ? t("assignee.you") : member.name,
    }));

  const searchHint =
    check.kind === "too-short"
      ? t("search.tooShort")
      : check.kind === "too-long"
        ? t("search.tooLong")
        : null;

  const emptyTitle = term
    ? t.rich("empty.search", { term: () => <bdi>{term}</bdi> })
    : view.unread
      ? t("empty.unread")
      : view.assignedTo === "me"
        ? t("empty.mine")
        : view.assignedTo === "none"
          ? t("empty.unassigned")
          : view.status === "closed"
            ? t("empty.closed")
            : t("empty.open");

  let state: React.ReactNode = null;
  if (list.isError) {
    const error = list.error;
    state = (
      <ErrorState
        headingLevel={2}
        title={t("error.title")}
        description={error instanceof ApiError ? errors(error.code) : errors(null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void list.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (!list.isPending && shown.length === 0) {
    state = (
      <EmptyState
        headingLevel={2}
        title={emptyTitle}
        action={
          term || view.unread || view.assignedTo || view.mode || view.status ? (
            <Button
              variant="secondary"
              onClick={() => {
                setTyped("");
                setTerm(null);
                go({ status: null, assignedTo: null, mode: null, priority: null, unread: false });
              }}
            >
              {t("empty.showAll")}
            </Button>
          ) : undefined
        }
      />
    );
  }

  return (
    <section
      aria-labelledby="inbox-list-heading"
      data-inbox-pane="list"
      className="flex min-h-0 flex-col gap-3 bg-secondary-subtle p-4"
    >
      <h1
        id="inbox-list-heading"
        className="m-0 text-2xl font-semibold tracking-tight text-text"
        tabIndex={-1}
      >
        {t("title")}
      </h1>

      <div className="flex flex-col gap-1">
        <label htmlFor={searchId} className="text-sm font-medium text-text">
          {t("search.label")}
        </label>
        <div className="flex items-center gap-2">
          <Input
            id={searchId}
            type="search"
            value={typed}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setTyped(event.target.value)}
            aria-describedby={searchHint ? `${searchId}-hint` : undefined}
            data-inbox-search=""
          />
          {typed ? (
            <Button variant="ghost" onClick={() => setTyped("")}>
              {t("search.clear")}
            </Button>
          ) : null}
        </div>
        {searchHint ? (
          <p id={`${searchId}-hint`} className="m-0 text-sm text-text-muted">
            {searchHint}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2" role="group" aria-label={t("filters.label")}>
        <SegmentedControl
          label={t("filters.status")}
          value={view.status ?? "default"}
          onChange={(value) =>
            go({ ...view, status: value === "default" ? null : (value as InboxView["status"]) })
          }
          options={[
            { value: "default", label: t("status.default") },
            ...STATUSES.filter((status) => status !== "open").map((status) => ({
              value: status,
              label: t(`status.${status}`),
            })),
          ]}
        />
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1">
            <span id={`${searchId}-assignee`} className="text-sm font-medium text-text">
              {t("filters.assignee")}
            </span>
            <Select
              aria-labelledby={`${searchId}-assignee`}
              value={view.assignedTo ?? "everyone"}
              onValueChange={(value) =>
                go({ ...view, assignedTo: value === "everyone" ? null : value })
              }
              options={[
                { value: "everyone", label: t("filters.everyone") },
                { value: "me", label: t("filters.mine") },
                { value: "none", label: t("filters.unassigned") },
                ...memberOptions.filter((option) => option.value !== profile?.id),
              ]}
            />
          </div>
          <div className="flex flex-col gap-1">
            <span id={`${searchId}-mode`} className="text-sm font-medium text-text">
              {t("filters.mode")}
            </span>
            <Select
              aria-labelledby={`${searchId}-mode`}
              value={view.mode ?? "any"}
              onValueChange={(value) =>
                go({ ...view, mode: value === "any" ? null : (value as InboxView["mode"]) })
              }
              options={[
                { value: "any", label: t("filters.anyMode") },
                { value: "ai", label: t("mode.ai") },
                { value: "human", label: t("mode.human") },
              ]}
            />
          </div>
        </div>
        <Button
          variant={view.unread ? "secondary" : "ghost"}
          aria-pressed={view.unread}
          onClick={() => go({ ...view, unread: !view.unread })}
          data-unread-toggle=""
        >
          {t("filters.unreadOnly")}
        </Button>
      </div>

      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto" tabIndex={-1}>
        <ConversationList
          label={t("listLabel")}
          loading={list.isPending}
          loadingLabel={t("loading")}
          state={state}
          {...(list.hasNextPage ? { loadMoreLabel: t("loadMore") } : {})}
          onLoadMore={() => void list.fetchNextPage()}
          loadingMore={list.isFetchingNextPage}
          updateLabel={waiting ? t("updated") : null}
          onShowUpdate={() => {
            if (waiting) setShown(waiting);
            setWaiting(null);
            listRef.current?.scrollTo({ top: 0 });
          }}
          sentinelRef={sentinelRef}
        >
          {shown.map((conversation) => {
            const who = whoOf(conversation) ?? t("unknownCustomer");
            const assignee = conversation.assigned_to_id
              ? conversation.assigned_to_id === profile?.id
                ? t("assignee.you")
                : (members.byId.get(conversation.assigned_to_id)?.name ?? t("assignee.someone"))
              : t("assignee.none");
            const last = conversation.last_message;
            const prefix = !last
              ? null
              : last.kind === "template"
                ? t("preview.template")
                : last.excerpt
                  ? last.direction === "outbound"
                    ? t("preview.reply")
                    : null
                  : t(`preview.kind.${previewKind(last.kind)}`);
            const unreadCount = conversation.unread_count ?? 0;
            const unread = unreadCount > 0 ? capCount(unreadCount, 99) : null;
            const activity = conversation.last_message_at ?? conversation.created_at;
            const time = relativeTime(activity, now, locale);
            const stateWords = [
              t(`status.${conversation.status}`),
              t(`mode.${conversation.mode}`),
              unread ? t("unreadCount", { count: unreadCount }) : null,
            ].filter(Boolean);
            return (
              <ConversationRow
                key={conversation.id}
                href={inboxHref(view, conversation.id)}
                who={who}
                handle={handleLine(conversation)}
                channelIcon={MessageCircle}
                channelIconName="MessageCircle"
                channelLabel={t(`channel.${conversation.channel}`)}
                number={numbers.numberOf(conversation.connection_id)}
                previewPrefix={prefix}
                previewText={last?.excerpt ?? null}
                time={time}
                timeFull={fullTime(activity, locale)}
                unread={unread}
                unreadLabel={unread ? t("unreadCount", { count: unreadCount }) : null}
                selected={conversation.id === openId}
                accessibleName={`${who}, ${stateWords.join(", ")}, ${time}`}
                badges={
                  <>
                    <Badge tone="neutral" icon={false}>
                      {t(`status.${conversation.status}`)}
                    </Badge>
                    <Badge tone="info" icon={false}>
                      {t(`mode.${conversation.mode}`)}
                    </Badge>
                    {conversation.priority !== "normal" ? (
                      <Badge tone="warning">{t(`priority.${conversation.priority}`)}</Badge>
                    ) : null}
                    <span className="text-xs text-text-muted">
                      <bdi>{assignee}</bdi>
                    </span>
                  </>
                }
                renderLink={({ href, children, ...rest }) => (
                  <NextLink href={href} {...rest}>
                    {children}
                  </NextLink>
                )}
                menu={
                  <DropdownMenu
                    trigger={
                      <IconButton
                        icon={MoreVertical}
                        iconName="MoreVertical"
                        label={t("rowMenu", { who })}
                      />
                    }
                    items={[
                      {
                        type: "item",
                        key: "unread",
                        label: t("markUnread"),
                        onSelect: () => onMarkUnread(conversation.id),
                      },
                    ]}
                  />
                }
              />
            );
          })}
        </ConversationList>
        {list.data ? (
          <p className="sr-only" role="status">
            {t("loadedCount", { count: shown.length, shown: number(shown.length) })}
          </p>
        ) : null}
      </div>
    </section>
  );
}

export function previewKind(
  kind: string,
): "image" | "audio" | "video" | "document" | "location" | "other" {
  if (
    kind === "image" ||
    kind === "audio" ||
    kind === "video" ||
    kind === "document" ||
    kind === "location"
  ) {
    return kind;
  }
  return "other";
}
