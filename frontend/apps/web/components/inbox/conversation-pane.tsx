"use client";

/**
 * APP-INBOX-02's frame (TH-01, CX-01 .. CX-05): the conversation's header with its state and
 * actions - take over / hand back, assign with the expected assignee, priority, close / reopen,
 * mark as unread - none of them optimistic; the thread; the composer.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MoreVertical, PanelRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { Badge, Button, DropdownMenu, EmptyState, IconButton, type MenuEntry } from "@wasla/ui";

import { conversationKey } from "../../lib/inbox/keys";
import { refreshInboxLists } from "../../lib/stream-handlers";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { ConversationComposer, useSender } from "./composer";
import { useCodeText } from "./error-text";
import { useInboxRuntime } from "./inbox-runtime";
import { handleLine, useMembers, whoOf, type ConversationRead } from "./people";
import { Thread, usePending } from "./thread";

export function useConversation(id: string | null) {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useQuery({
    queryKey: conversationKey(scope, id ?? "none"),
    queryFn: ({ signal }) =>
      unwrap(
        session.api.GET("/api/v1/conversations/{conversation_id}", {
          params: { path: { conversation_id: id! } },
          signal,
        }),
      ) as Promise<ConversationRead>,
    enabled: Boolean(id),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
}

export function ConversationPane({
  id,
  inView,
  onBack,
  onShowPanel,
  panelShown,
  onClosedUnread,
}: {
  id: string;
  /** Whether the open conversation is still in the list's view (IL-06). */
  inView: boolean;
  onBack: (() => void) | null;
  onShowPanel: () => void;
  panelShown: boolean;
  onClosedUnread: () => void;
}) {
  const t = useTranslations("app.inbox");
  const errors = useCodeText();
  const { session, profile } = useSession();
  const { scope } = useWorkspace();
  const queryClient = useQueryClient();
  const runtime = useInboxRuntime();
  const members = useMembers();
  const conversation = useConversation(id);
  const [notice, setNotice] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    runtime.setOpenConversation(id);
    return () => runtime.setOpenConversation(null);
  }, [id, runtime]);

  const apply = (next: ConversationRead) => {
    queryClient.setQueryData(conversationKey(scope, id), next);
    refreshInboxLists(queryClient, scope);
  };

  const action = useMutation({
    mutationFn: async (
      input:
        | { kind: "mode"; mode: "ai" | "human" }
        | { kind: "assign"; to: string | null; expected: string | null }
        | { kind: "priority"; priority: "normal" | "high" | "urgent" }
        | { kind: "close" | "reopen" },
    ) => {
      const path = { conversation_id: id };
      switch (input.kind) {
        case "mode":
          return unwrap(
            session.api.POST("/api/v1/conversations/{conversation_id}/mode", {
              params: { path },
              body: { mode: input.mode },
            }),
          );
        case "assign":
          return unwrap(
            session.api.POST("/api/v1/conversations/{conversation_id}/assignment", {
              params: { path },
              body: { assigned_to_id: input.to, expected_assigned_to_id: input.expected },
            }),
          );
        case "priority":
          return unwrap(
            session.api.POST("/api/v1/conversations/{conversation_id}/priority", {
              params: { path },
              body: { priority: input.priority },
            }),
          );
        case "close":
          return unwrap(
            session.api.POST("/api/v1/conversations/{conversation_id}/close", { params: { path } }),
          );
        case "reopen":
          return unwrap(
            session.api.POST("/api/v1/conversations/{conversation_id}/reopen", {
              params: { path },
            }),
          );
      }
    },
    onSuccess: (next) => {
      setNotice(null);
      apply(next);
    },
    onError: async (error) => {
      if (
        error instanceof ApiError &&
        (error.code === "stale_assignment" || error.status === 409)
      ) {
        // CX-02: never a silent overwrite - read it again and say who has it now.
        const fresh = await queryClient.fetchQuery({
          queryKey: conversationKey(scope, id),
          queryFn: () =>
            unwrap(
              session.api.GET("/api/v1/conversations/{conversation_id}", {
                params: { path: { conversation_id: id } },
              }),
            ),
          staleTime: 0,
        });
        const owner = fresh.assigned_to_id;
        const name = owner
          ? owner === profile?.id
            ? t("assignee.you")
            : (members.byId.get(owner)?.name ?? t("assignee.someone"))
          : t("assignee.none");
        setNotice(t("staleAssignee", { name }));
        return;
      }
      setNotice(error instanceof ApiError ? errors(error.code) : errors(null));
    },
  });

  const markUnread = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/conversations/{conversation_id}/unread", {
          params: { path: { conversation_id: id } },
        }),
      ),
    onSuccess: () => {
      refreshInboxLists(queryClient, scope);
      onClosedUnread();
    },
  });

  const sender = useSender(conversation.data ?? ({ id } as ConversationRead));
  // CP-06: the API answers an opted-out customer on a send; the composer then stops.
  const optedOut = usePending(id).some((send) => send.errorCode === "recipient_opted_out");

  if (conversation.isPending) {
    return (
      <p role="status" className="m-0 p-5 text-sm text-text-muted">
        {t("thread.loading")}
      </p>
    );
  }
  if (conversation.isError) {
    const notFound = conversation.error instanceof ApiError && conversation.error.status === 404;
    return (
      <div className="p-5">
        <EmptyState
          headingLevel={2}
          title={notFound ? t("notFound.title") : t("error.title")}
          description={notFound ? t("notFound.description") : undefined}
          action={
            onBack ? (
              <Button variant="secondary" onClick={onBack}>
                {t("back")}
              </Button>
            ) : undefined
          }
        />
      </div>
    );
  }

  const data = conversation.data;
  const who = whoOf(data) ?? t("unknownCustomer");
  const handle = handleLine(data);
  const assignee = data.assigned_to_id
    ? data.assigned_to_id === profile?.id
      ? t("assignee.you")
      : (members.byId.get(data.assigned_to_id)?.name ?? t("assignee.someone"))
    : t("assignee.none");
  const busy = action.isPending;
  const expected = data.assigned_to_id;

  const assignItems: MenuEntry[] = [
    ...(profile && data.assigned_to_id !== profile.id
      ? [
          {
            type: "item" as const,
            key: "me",
            label: t("assignToMe"),
            onSelect: () => action.mutate({ kind: "assign", to: profile.id, expected }),
          },
        ]
      : []),
    ...[...members.byId.entries()]
      .filter(([memberId]) => memberId !== profile?.id && memberId !== data.assigned_to_id)
      .map(([memberId, member]) => ({
        type: "item" as const,
        key: memberId,
        label: <bdi>{member.name}</bdi>,
        onSelect: () => action.mutate({ kind: "assign", to: memberId, expected }),
      })),
    ...(data.assigned_to_id
      ? [
          { type: "separator" as const, key: "sep" },
          {
            type: "item" as const,
            key: "none",
            label: t("unassign"),
            onSelect: () => action.mutate({ kind: "assign", to: null, expected }),
          },
        ]
      : []),
  ];

  const moreItems: MenuEntry[] = [
    {
      type: "submenu",
      key: "priority",
      label: t("priorityMenu"),
      items: (["normal", "high", "urgent"] as const).map((priority) => ({
        type: "item" as const,
        key: priority,
        label: t(`priority.${priority}`),
        disabled: data.priority === priority,
        onSelect: () => action.mutate({ kind: "priority", priority }),
      })),
    },
    { type: "item", key: "unread", label: t("markUnread"), onSelect: () => markUnread.mutate() },
    data.status === "closed"
      ? {
          type: "item",
          key: "reopen",
          label: t("reopen"),
          onSelect: () => action.mutate({ kind: "reopen" }),
        }
      : {
          type: "item",
          key: "close",
          label: t("close"),
          onSelect: () => action.mutate({ kind: "close" }),
        },
  ];

  return (
    <section
      aria-labelledby="inbox-conversation-heading"
      data-inbox-pane="conversation"
      className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas"
    >
      <header
        className="flex flex-col gap-2 border-b border-border bg-surface px-4 py-3"
        aria-busy={busy || undefined}
      >
        <div className="flex items-center gap-2">
          {onBack ? (
            <IconButton icon={ArrowLeft} iconName="ArrowLeft" label={t("back")} onClick={onBack} />
          ) : null}
          <div className="min-w-0 flex-1">
            <h2
              id="inbox-conversation-heading"
              ref={headingRef}
              tabIndex={-1}
              className="m-0 truncate text-lg font-semibold text-text outline-none"
            >
              <bdi>{who}</bdi>
            </h2>
            {handle ? (
              <p className="m-0 truncate text-sm text-text-muted">
                <bdi dir="ltr">{handle}</bdi>
              </p>
            ) : null}
          </div>
          {!panelShown ? (
            <IconButton
              icon={PanelRight}
              iconName="PanelRight"
              label={t("panel.show")}
              onClick={onShowPanel}
              data-show-panel=""
            />
          ) : null}
          <DropdownMenu
            trigger={
              <IconButton
                icon={MoreVertical}
                iconName="MoreVertical"
                label={t("more")}
                data-conversation-menu=""
              />
            }
            items={moreItems}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="neutral" icon={false}>
            {t(`status.${data.status}`)}
          </Badge>
          <Badge tone="info" icon={false}>
            {t(`mode.${data.mode}`)}
          </Badge>
          {data.priority !== "normal" ? (
            <Badge tone="warning">{t(`priority.${data.priority}`)}</Badge>
          ) : null}
          <span className="text-sm text-text-muted">
            {data.assigned_to_id ? (
              // FEF-33: one ICU message, the name isolated inside it.
              <span data-assigned-to="">
                {t.rich("assignedTo", { name: () => <bdi>{assignee}</bdi> })}
              </span>
            ) : (
              assignee
            )}
          </span>
          <span className="ms-auto flex flex-wrap gap-2">
            <Button
              variant="secondary"
              busy={busy && action.variables?.kind === "mode"}
              disabled={busy}
              onClick={() =>
                action.mutate({ kind: "mode", mode: data.mode === "ai" ? "human" : "ai" })
              }
              data-mode-action=""
            >
              {data.mode === "ai" ? t("takeOver") : t("handBack")}
            </Button>
            <DropdownMenu
              trigger={
                <Button
                  variant="secondary"
                  disabled={busy}
                  busy={busy && action.variables?.kind === "assign"}
                  data-assign=""
                >
                  {t("assign")}
                </Button>
              }
              items={assignItems}
            />
          </span>
        </div>
        {notice ? (
          <p role="status" className="m-0 text-sm text-text" data-action-notice="">
            {notice}
          </p>
        ) : null}
        {!inView ? <p className="m-0 text-sm text-text-muted">{t("notInView")}</p> : null}
      </header>
      <Thread conversation={data} onRetry={sender.retry} />
      <ConversationComposer
        conversation={data}
        optedOut={optedOut}
        onReopen={() => action.mutate({ kind: "reopen" })}
        sender={sender}
      />
    </section>
  );
}
