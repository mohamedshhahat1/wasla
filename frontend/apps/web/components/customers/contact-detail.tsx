"use client";

/**
 * APP-CUS-01, the detail (CL-03): who the customer is - one GET /contacts/{id} - the name the team
 * gives them (PATCH, the name only), their marketing consent channel by channel with its history,
 * every conversation with them and every lead they have. Nothing here is optimistic: each change
 * shows its progress on its own control and the page reads what is now stored. A control a role
 * cannot use is absent, never disabled - a member sees no "Clear opt-out".
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageCircle } from "lucide-react";
import NextLink from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConsentHistory,
  ConversationRow,
  Dialog,
  EmptyState,
  ErrorState,
  IdentityList,
  InlineEditField,
  LeadRow,
  RecordHeader,
  RecordList,
  buttonClasses,
} from "@wasla/ui";

import {
  CONTACTS,
  consentEventsKey,
  contactConversationsKey,
  contactKey,
  contactLeadsKey,
  resourcePrefix,
} from "../../lib/customers/keys";
import { CONTACT_NAME_MAX } from "../../lib/customers/lead-form";
import { leadHref, OPEN_LEAD_STATUSES } from "../../lib/customers/view";
import { CONVERSATION, INBOX } from "../../lib/inbox/keys";
import { fullTime } from "../../lib/inbox/time";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { LeadCreateDialog } from "./lead-create-dialog";
import {
  codeOf,
  isNotFound,
  LeadStatusBadge,
  OptedOutBadge,
  PAGE_SIZE,
  rowLink,
  useCounter,
  useIsAdmin,
  useLeadFacts,
  useTimes,
  type ConsentEvent,
  type ContactRead,
} from "./shared";

type Channel = ContactRead["channels"][number]["channel"];

/** The sentence of one consent event: what happened and who did it, one ICU message each. */
export function useConsentSentence() {
  const t = useTranslations("app.customers");
  return (event: ConsentEvent) => {
    const name = () => <bdi>{event.by?.display_name ?? ""}</bdi>;
    if (event.before_history) return t("consent.event.beforeHistory");
    const cleared = event.action === "opt_out_cleared";
    if (event.source === "customer") {
      return cleared ? t("consent.event.clearedByCustomer") : t("consent.event.optedOutByCustomer");
    }
    if (event.by?.display_name) {
      return cleared
        ? t.rich("consent.event.clearedBy", { name })
        : t.rich("consent.event.optedOutBy", { name });
    }
    return cleared ? t("consent.event.clearedByTeam") : t("consent.event.optedOutByTeam");
  };
}

export function ContactDetail({ id }: { id: string }) {
  const t = useTranslations("app.customers");
  const inbox = useTranslations("app.inbox");
  const errors = useCodeText();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const admin = useIsAdmin();
  const times = useTimes();
  const counter = useCounter();
  const leadFacts = useLeadFacts();
  const consentSentence = useConsentSentence();
  const headingId = useId();
  const confirmId = useId();
  const [copied, setCopied] = useState(false);
  const [recording, setRecording] = useState<Channel | null>(null);
  const [clearing, setClearing] = useState<Channel | null>(null);
  const [asked, setAsked] = useState(false);
  const [creating, setCreating] = useState(false);

  const contact = useQuery({
    queryKey: contactKey(scope, id),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/contacts/{contact_id}", { params: { path: { contact_id: id } } }),
      ),
    retry: (count, error) => !isNotFound(error) && count < 2,
    refetchOnWindowFocus: true,
  });
  const found = contact.isSuccess;

  const history = useInfiniteQuery({
    queryKey: consentEventsKey(scope, id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/contacts/{contact_id}/consent-events", {
          params: {
            path: { contact_id: id },
            query: { limit: 50, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: found,
    refetchOnWindowFocus: true,
  });
  const conversations = useInfiniteQuery({
    queryKey: contactConversationsKey(scope, id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/conversations", {
          params: {
            query: {
              contact_id: id,
              status: "all",
              limit: PAGE_SIZE,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: found,
    refetchOnWindowFocus: true,
  });
  const leads = useInfiniteQuery({
    queryKey: contactLeadsKey(scope, id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/leads", {
          params: {
            query: {
              contact_id: id,
              limit: PAGE_SIZE,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: found,
    refetchOnWindowFocus: true,
  });

  /** After a change: this page's reads, and every loaded conversation and list that shows the customer. */
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: contactKey(scope, id) });
    void queryClient.invalidateQueries({ queryKey: consentEventsKey(scope, id) });
    void queryClient.invalidateQueries({ queryKey: contactConversationsKey(scope, id) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONTACTS) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONVERSATION) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, INBOX) });
  };

  const rename = useMutation({
    mutationFn: (name: string | null) =>
      unwrap(
        session.api.PATCH("/api/v1/contacts/{contact_id}", {
          params: { path: { contact_id: id } },
          body: { name },
        }),
      ),
    onSuccess: (data) => {
      queryClient.setQueryData(contactKey(scope, id), data);
      refresh();
    },
  });
  const optOut = useMutation({
    mutationFn: (channel: Channel) =>
      unwrap(
        session.api.POST("/api/v1/contacts/{contact_id}/opt-out", {
          params: { path: { contact_id: id } },
          body: { channel, source: "team" },
        }),
      ),
    onSuccess: () => {
      setRecording(null);
      refresh();
    },
  });
  const clear = useMutation({
    mutationFn: (channel: Channel) =>
      unwrap(
        session.api.DELETE("/api/v1/contacts/{contact_id}/opt-out", {
          params: { path: { contact_id: id }, query: { channel } },
        }),
      ),
    onSuccess: () => {
      setClearing(null);
      setAsked(false);
      refresh();
    },
  });

  if (contact.isError) {
    const error = contact.error;
    if (isNotFound(error)) {
      return (
        <EmptyState
          headingLevel={1}
          title={t("contact.notFoundTitle")}
          description={t("contact.notFoundBody")}
          action={
            <NextLink href="/contacts" className={buttonClasses("secondary")}>
              {t("contact.back")}
            </NextLink>
          }
        />
      );
    }
    return (
      <ErrorState
        headingLevel={1}
        title={t("contact.errorTitle")}
        description={errors(codeOf(error))}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void contact.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  }
  if (contact.isPending) {
    return (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("contact.loading")}
      </p>
    );
  }

  const data = contact.data;
  const phone = data.identities.find((identity) => identity.kind === "phone")?.value ?? null;
  const who = data.display_name || phone || t("contacts.unnamed");
  const events = history.data?.pages.flatMap((page) => page.items) ?? [];
  const conversationRows = conversations.data?.pages.flatMap((page) => page.items) ?? [];
  const leadRows = leads.data?.pages.flatMap((page) => page.items) ?? [];
  const hasOpenLead = leadRows.some((lead) =>
    (OPEN_LEAD_STATUSES as readonly string[]).includes(lead.status),
  );
  const more = (query: {
    hasNextPage: boolean;
    isFetchingNextPage: boolean;
    fetchNextPage: () => unknown;
  }) =>
    query.hasNextPage ? (
      <Button
        variant="secondary"
        busy={query.isFetchingNextPage}
        onClick={() => void query.fetchNextPage()}
      >
        {t("loadMore")}
      </Button>
    ) : null;

  return (
    <div className="flex flex-col gap-6" data-screen="contact" data-contact-id={data.id}>
      <NextLink href="/contacts" className="self-start text-sm font-medium text-text underline">
        {t("contact.back")}
      </NextLink>
      <RecordHeader
        title={who}
        titleId={headingId}
        beside={data.opted_out ? <OptedOutBadge /> : null}
        sub={
          data.name && data.profile_name ? (
            <span data-profile-name="">
              {t.rich("contact.profileName", { name: () => <bdi>{data.profile_name}</bdi> })}
            </span>
          ) : null
        }
        facts={
          <div className="flex flex-col gap-3" data-contact-name="">
            <InlineEditField
              label={t("contact.nameLabel")}
              value={data.name ?? ""}
              editLabel={data.name ? t("contact.editName") : t("contact.giveName")}
              saveLabel={t("save")}
              cancelLabel={t("cancel")}
              maxLength={CONTACT_NAME_MAX}
              counter={counter}
              error={rename.isError ? errors(codeOf(rename.error)) : undefined}
              onReset={() => rename.reset()}
              onSave={(value) => (value ? rename.mutateAsync(value) : rename.mutateAsync(null))}
            >
              {data.name ? (
                <Button
                  variant="ghost"
                  busy={rename.isPending}
                  onClick={() => rename.mutate(null)}
                  data-use-profile-name=""
                >
                  {t("contact.useProfileName")}
                </Button>
              ) : null}
            </InlineEditField>
            <p className="m-0 text-sm text-text-muted">{t("contact.nameHint")}</p>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="flex flex-col gap-6">
          <Card as="section" aria-labelledby={`${headingId}-who`} data-section="identities">
            <h2 id={`${headingId}-who`} className="m-0 mb-4 text-xl font-semibold text-text">
              {t("contact.identities")}
            </h2>
            <IdentityList
              label={t("contact.identities")}
              items={data.identities.map((identity) => ({
                key: identity.id,
                channel: inbox(`channel.${identity.channel}` as "channel.whatsapp"),
                kind: t(`identityKind.${identity.kind}` as "identityKind.phone"),
                value: identity.value,
                copyLabel: t("contact.copy"),
              }))}
              onCopy={(value) => {
                void navigator.clipboard?.writeText(value).then(
                  () => setCopied(true),
                  () => undefined,
                );
              }}
            />
            <p role="status" className="m-0 mt-2 text-sm text-text-muted">
              {copied ? t("contact.copied") : ""}
            </p>
            <p className="m-0 mt-2 text-sm text-text-muted">
              {t("contact.firstSeen", { date: fullTime(data.created_at, locale) })}
            </p>
          </Card>

          <Card as="section" aria-labelledby={`${headingId}-consent`} data-section="consent">
            <h2 id={`${headingId}-consent`} className="m-0 mb-4 text-xl font-semibold text-text">
              {t("consent.title")}
            </h2>
            <ul className="m-0 flex list-none flex-col gap-4 p-0">
              {data.channels.map((entry) => {
                const out = entry.marketing_opt_out_at;
                return (
                  <li
                    key={entry.channel}
                    data-consent-channel={entry.channel}
                    data-consent-state={out ? "opted-out" : "receiving"}
                    className="flex flex-wrap items-center justify-between gap-3"
                  >
                    <span className="flex flex-col gap-1">
                      <span className="text-sm text-text-muted">
                        {inbox(`channel.${entry.channel}` as "channel.whatsapp")}
                      </span>
                      <span className="flex flex-wrap items-center gap-2 text-md text-text">
                        {out ? (
                          <>
                            <Badge tone="warning">{t("consent.optedOutBadge")}</Badge>
                            {t("consent.optedOutSince", { date: fullTime(out, locale) })}
                          </>
                        ) : (
                          t("consent.receiving")
                        )}
                      </span>
                    </span>
                    {out ? (
                      admin ? (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            clear.reset();
                            setAsked(false);
                            setClearing(entry.channel);
                          }}
                          data-clear-opt-out=""
                        >
                          {t("consent.clear")}
                        </Button>
                      ) : null
                    ) : (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          optOut.reset();
                          setRecording(entry.channel);
                        }}
                        data-record-opt-out=""
                      >
                        {t("consent.record")}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
            <h3 className="m-0 mb-3 mt-6 text-md font-semibold text-text">
              {t("consent.history")}
            </h3>
            <ConsentHistory
              label={t("consent.history")}
              empty={t("consent.historyEmpty")}
              entries={events.map((event, index) => {
                const at = times(event.at);
                return {
                  key: event.id ?? `before-${index}`,
                  what: consentSentence(event),
                  channel: inbox(`channel.${event.channel}` as "channel.whatsapp"),
                  time: at?.time ?? "",
                  timeFull: at?.full ?? "",
                };
              })}
              footer={more(history)}
            />
          </Card>
        </div>

        <div className="flex flex-col gap-6">
          <Card as="section" aria-labelledby={`${headingId}-conv`} data-section="conversations">
            <h2 id={`${headingId}-conv`} className="m-0 mb-4 text-xl font-semibold text-text">
              {t("contact.conversations")}
            </h2>
            {conversationRows.length ? (
              <RecordList label={t("contact.conversations")} footer={more(conversations)}>
                {conversationRows.map((conversation) => {
                  const last = conversation.last_message ?? null;
                  const at = times(conversation.last_message_at ?? conversation.created_at);
                  const unread = conversation.unread_count ?? 0;
                  return (
                    <ConversationRow
                      key={conversation.id}
                      href={`/inbox/${conversation.id}`}
                      who={who}
                      channelIcon={MessageCircle}
                      channelIconName="MessageCircle"
                      channelLabel={inbox(`channel.${conversation.channel}` as "channel.whatsapp")}
                      previewText={last?.excerpt ?? null}
                      time={at?.time ?? ""}
                      timeFull={at?.full ?? ""}
                      unread={unread > 0 ? (unread > 99 ? "99+" : String(unread)) : null}
                      unreadLabel={unread > 0 ? inbox("unreadCount", { count: unread }) : null}
                      badges={
                        <Badge tone="neutral" icon={false}>
                          {inbox(`status.${conversation.status}` as "status.open")}
                        </Badge>
                      }
                      renderLink={({ href, children, ...rest }) => (
                        <NextLink href={href} {...rest}>
                          {children}
                        </NextLink>
                      )}
                    />
                  );
                })}
              </RecordList>
            ) : (
              <p className="m-0 text-sm text-text-muted">
                {conversations.isPending ? t("loading") : t("contact.noConversations")}
              </p>
            )}
          </Card>

          <Card as="section" aria-labelledby={`${headingId}-leads`} data-section="leads">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <h2 id={`${headingId}-leads`} className="m-0 text-xl font-semibold text-text">
                {t("contact.leads")}
              </h2>
              {leads.isSuccess && !hasOpenLead ? (
                <Button variant="secondary" onClick={() => setCreating(true)} data-create-lead="">
                  {t("create.open")}
                </Button>
              ) : null}
            </div>
            {leadRows.length ? (
              <RecordList label={t("contact.leads")} footer={more(leads)}>
                {leadRows.map((lead) => {
                  const at = times(lead.last_activity_at ?? lead.updated_at);
                  return (
                    <LeadRow
                      key={lead.id}
                      href={leadHref(lead.id)}
                      who={lead.name || data.display_name || t("leads.unnamed")}
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
            ) : (
              <p className="m-0 text-sm text-text-muted">
                {leads.isPending ? t("loading") : t("contact.noLeads")}
              </p>
            )}
          </Card>
        </div>
      </div>

      <Dialog
        open={recording !== null}
        onOpenChange={(open) => {
          if (!open) setRecording(null);
        }}
        title={t("consent.recordTitle")}
        description={t("consent.recordBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRecording(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              busy={optOut.isPending}
              onClick={() => recording && optOut.mutate(recording)}
              data-confirm-opt-out=""
            >
              {t("consent.record")}
            </Button>
          </>
        }
      >
        {optOut.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(codeOf(optOut.error))}
          </p>
        ) : null}
      </Dialog>

      <Dialog
        open={clearing !== null}
        onOpenChange={(open) => {
          if (!open) setClearing(null);
        }}
        title={t("consent.clearTitle")}
        description={t("consent.clearBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setClearing(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!asked}
              busy={clear.isPending}
              onClick={() => clearing && asked && clear.mutate(clearing)}
              data-confirm-clear=""
            >
              {t("consent.clear")}
            </Button>
          </>
        }
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id={confirmId}
            checked={asked}
            onCheckedChange={(value) => setAsked(value === true)}
          />
          <label htmlFor={confirmId} className="text-md text-text">
            {t("consent.clearConfirm")}
          </label>
        </div>
        {clear.isError ? (
          <p role="alert" className="m-0 mt-3 text-sm font-medium text-danger">
            {errors(codeOf(clear.error))}
          </p>
        ) : null}
      </Dialog>

      {creating ? (
        <LeadCreateDialog
          open
          onOpenChange={setCreating}
          contactId={data.id}
          initialName={data.display_name}
        />
      ) : null}
    </div>
  );
}
