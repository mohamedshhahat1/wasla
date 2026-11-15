"use client";

/**
 * APP-INBOX-02's history (TH-01, TH-02, IL-07): newest at the bottom, earlier pages above with the
 * anchor kept, the optimistic bubbles of this tab after them, links behind a confirmation, media
 * tiles, "Seen by", the "New messages" pill, and the team's read position moved only while the
 * newest message is in view in a visible tab.
 */
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { unwrap } from "@wasla/api-client";
import {
  Badge,
  Button,
  InertLink,
  LinkConfirm,
  MediaViewer,
  MessageBubble,
  NewMessagesPill,
  ThreadNote,
  type DeliveryState,
} from "@wasla/ui";

import { conversationKey, messagesKey } from "../../lib/inbox/keys";
import { mayMarkRead, READ_DEBOUNCE_MS } from "../../lib/inbox/live";
import { dedupeById, type PendingSend } from "../../lib/inbox/send-queue";
import { openConfirmed, splitLinks } from "../../lib/inbox/text";
import { clockTime, dayLabel, fullTime, threadItems } from "../../lib/inbox/time";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { useCodeText } from "./error-text";
import { useInboxRuntime } from "./inbox-runtime";
import { MessageMedia } from "./media";
import { whoOf, type ConversationRead, type MessageRead } from "./people";

export const MESSAGE_PAGE = 30;

type Page = { items: MessageRead[]; next_cursor: string | null };

interface Drawn {
  id: string;
  side: "in" | "out";
  createdAt: string;
  message?: MessageRead;
  pending?: PendingSend;
}

function useVisible(): boolean {
  return useSyncExternalStore(
    (notify) => {
      document.addEventListener("visibilitychange", notify);
      return () => document.removeEventListener("visibilitychange", notify);
    },
    () => document.visibilityState === "visible",
    () => true,
  );
}

export function usePending(conversationId: string): PendingSend[] {
  const { queue } = useInboxRuntime();
  const subscribe = useCallback((notify: () => void) => queue.subscribe(notify), [queue]);
  const snapshot = useRef<{ key: string; value: PendingSend[] }>({ key: "", value: [] });
  return useSyncExternalStore(
    subscribe,
    () => {
      const value = queue.forConversation(conversationId);
      const key = value
        .map((send) => `${send.clientId}:${send.status}:${send.errorCode ?? ""}`)
        .join("|");
      if (key !== snapshot.current.key) snapshot.current = { key, value };
      return snapshot.current.value;
    },
    () => snapshot.current.value,
  );
}

export function Thread({
  conversation,
  onRetry,
}: {
  conversation: ConversationRead;
  onRetry: (send: PendingSend) => void;
}) {
  const t = useTranslations("app.inbox");
  const errors = useCodeText();
  const locale = useLocale();
  const { session, profile } = useSession();
  const { scope } = useWorkspace();
  const runtime = useInboxRuntime();
  const queryClient = useQueryClient();
  const visible = useVisible();
  const pending = usePending(conversation.id);
  const customer = whoOf(conversation) ?? t("unknownCustomer");

  const history = useInfiniteQuery({
    queryKey: messagesKey(scope, conversation.id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/conversations/{conversation_id}/messages", {
          params: {
            path: { conversation_id: conversation.id },
            query: { limit: MESSAGE_PAGE, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ) as Promise<Page>,
    getNextPageParam: (last) => last.next_cursor ?? null,
  });

  const loaded = useMemo(
    () => dedupeById(history.data?.pages.flatMap((page) => page.items) ?? []).reverse(),
    [history.data],
  );
  useEffect(
    () => runtime.queue.reconcile(loaded.map((message) => message.id)),
    [loaded, runtime.queue],
  );

  const drawn: Drawn[] = useMemo(() => {
    const settled = new Set(loaded.map((message) => message.id));
    return [
      ...loaded.map((message) => ({
        id: message.id,
        side: message.direction === "inbound" ? ("in" as const) : ("out" as const),
        createdAt: message.created_at,
        message,
      })),
      ...pending
        .filter((send) => !send.messageId || !settled.has(send.messageId))
        .map((send) => ({
          id: `pending-${send.clientId}`,
          side: "out" as const,
          createdAt: new Date(send.createdAt).toISOString(),
          pending: send,
        })),
    ];
  }, [loaded, pending]);
  const items = useMemo(() => threadItems(drawn), [drawn]);

  // Scrolling: newest at the bottom; the anchor kept when earlier pages arrive.
  const box = useRef<HTMLDivElement | null>(null);
  const bottom = useRef<HTMLLIElement | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const firstLoad = useRef(true);
  const lastNewest = useRef<string | null>(null);
  const prepend = useRef<{ height: number; top: number } | null>(null);

  useLayoutEffect(() => {
    const node = box.current;
    if (!node) return;
    if (prepend.current) {
      node.scrollTop = node.scrollHeight - prepend.current.height + prepend.current.top;
      prepend.current = null;
      return;
    }
    const newest = drawn.at(-1)?.id ?? null;
    if (firstLoad.current && drawn.length) {
      node.scrollTop = node.scrollHeight;
      firstLoad.current = false;
    } else if (newest !== lastNewest.current) {
      if (atBottom) node.scrollTop = node.scrollHeight;
      else if (drawn.at(-1)?.side === "in") setNewCount((count) => count + 1);
    }
    lastNewest.current = newest;
  }, [drawn, atBottom]);

  const onScroll = () => {
    const node = box.current;
    if (!node) return;
    const near = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
    setAtBottom(near);
    if (near) setNewCount(0);
  };

  const loadEarlier = () => {
    const node = box.current;
    if (node) prepend.current = { height: node.scrollHeight, top: node.scrollTop };
    void history.fetchNextPage();
  };

  // IL-07: mark read once the newest message is in view in a visible tab.
  const newestServer = loaded.at(-1)?.id ?? null;
  const seenNewest = useRef<string | null>(null);
  const markRead = useMutation({
    mutationFn: (messageId: string) =>
      unwrap(
        session.api.POST("/api/v1/conversations/{conversation_id}/read", {
          params: { path: { conversation_id: conversation.id } },
          body: { up_to_message_id: messageId },
        }),
      ),
    onSuccess: (answer) => {
      queryClient.setQueryData<ConversationRead>(
        conversationKey(scope, conversation.id),
        (current) =>
          current
            ? { ...current, unread_count: answer.unread_count, read: answer.read ?? null }
            : current,
      );
    },
  });
  useEffect(() => {
    if (
      !mayMarkRead({
        visible,
        newestInView: atBottom,
        newestMessageId: newestServer,
        readMessageId: conversation.read?.message_id ?? null,
        unreadCount: conversation.unread_count ?? 0,
        alreadySeen: seenNewest.current,
      })
    ) {
      // In view with nothing to send (already read, or a reply): this screen has seen it, so a
      // later "Mark as unread" by anyone is not undone here (FEF-35).
      if (visible && atBottom && newestServer && document.visibilityState === "visible") {
        seenNewest.current = newestServer;
      }
      return;
    }
    const timer = setTimeout(() => {
      if (!newestServer || document.visibilityState !== "visible") return;
      seenNewest.current = newestServer;
      markRead.mutate(newestServer);
    }, READ_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [visible, atBottom, newestServer, conversation.read?.message_id, conversation.unread_count]); // eslint-disable-line react-hooks/exhaustive-deps

  // Links behind a confirmation; the image viewer over the images already loaded.
  const [link, setLink] = useState<string | null>(null);
  const images = useRef(new Map<string, string | null>());
  const [viewer, setViewer] = useState<string | null>(null);
  const registerImage = useCallback((mediaId: string, src: string | null) => {
    if (src) images.current.set(mediaId, src);
    else images.current.delete(mediaId);
  }, []);
  const imageIds = [...images.current.entries()].filter(([, src]) => src).map(([id]) => id);
  const viewerIndex = viewer ? imageIds.indexOf(viewer) : -1;

  const authorOf = (message: MessageRead): string | null => {
    if (message.direction === "inbound") return null;
    if (message.origin === "agent" || message.origin === "follow_up") return t("author.ai");
    if (message.origin === "campaign") return t("author.campaign");
    if (message.sent_by_id && message.sent_by_id === profile?.id) return t("author.you");
    return t("author.member");
  };

  const deliveryOf = (status: string): { state: DeliveryState; label: string } | null => {
    if (status === "received") return null;
    const known = ["pending", "sent", "delivered", "read", "failed"] as const;
    const state = (known as readonly string[]).includes(status)
      ? (status as DeliveryState)
      : "pending";
    return { state, label: t(`delivery.${state}`) };
  };

  const renderText = (text: string) =>
    splitLinks(text).map((part, index) =>
      part.kind === "link" ? (
        <InertLink key={index} onActivate={() => setLink(part.url)}>
          {part.text}
        </InertLink>
      ) : (
        <span key={index}>{part.text}</span>
      ),
    );

  const seenIndex = conversation.read?.message_id
    ? items.findIndex(
        (item) => item.kind === "message" && item.message.id === conversation.read?.message_id,
      )
    : -1;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={box}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-3"
        data-thread=""
        tabIndex={-1}
      >
        {history.hasNextPage ? (
          <div className="flex justify-center py-2">
            <Button
              variant="ghost"
              onClick={loadEarlier}
              busy={history.isFetchingNextPage}
              data-load-earlier=""
            >
              {t("loadEarlier")}
            </Button>
          </div>
        ) : null}
        {history.isPending ? (
          <p role="status" className="m-0 text-sm text-text-muted">
            {t("thread.loading")}
          </p>
        ) : history.isError ? (
          <div className="flex flex-col items-start gap-2">
            <p className="m-0 text-sm text-text">{t("thread.error")}</p>
            <Button variant="secondary" onClick={() => void history.refetch()}>
              {t("retry")}
            </Button>
          </div>
        ) : (
          <ol
            className="m-0 flex list-none flex-col p-0"
            aria-label={t("thread.label", { who: customer })}
          >
            {items.map((item, index) => {
              if (item.kind === "day") {
                return <ThreadNote key={item.key}>{dayLabel(item.day, locale)}</ThreadNote>;
              }
              const entry = item.message;
              const nodes = [];
              if (entry.message) {
                const message = entry.message;
                nodes.push(
                  <MessageBubble
                    key={item.key}
                    messageId={message.id}
                    side={entry.side}
                    author={authorOf(message)}
                    marks={
                      message.kind === "template" ? (
                        <Badge tone="info" icon={false}>
                          {t("template")}
                        </Badge>
                      ) : null
                    }
                    media={
                      (message.media ?? []).length ? (
                        <>
                          {(message.media ?? []).map((media) => (
                            <MessageMedia
                              key={media.id}
                              conversationId={conversation.id}
                              media={media}
                              from={customer}
                              onOpenImage={setViewer}
                              registerImage={registerImage}
                            />
                          ))}
                        </>
                      ) : null
                    }
                    time={clockTime(message.created_at, locale)}
                    timeFull={fullTime(message.created_at, locale)}
                    delivery={entry.side === "out" ? deliveryOf(message.status) : null}
                    failure={message.status === "failed" ? { reason: t("failedSend") } : null}
                    firstOfGroup={item.firstOfGroup}
                    lastOfGroup={item.lastOfGroup}
                  >
                    {message.body &&
                    !(message.media?.length && message.media[0]?.caption === message.body) ? (
                      renderText(message.body)
                    ) : message.kind === "template" && message.template_name ? (
                      <bdi>{message.template_name}</bdi>
                    ) : null}
                  </MessageBubble>,
                );
              } else if (entry.pending) {
                const send = entry.pending;
                nodes.push(
                  <MessageBubble
                    key={item.key}
                    side="out"
                    author={t("author.you")}
                    firstOfGroup={item.firstOfGroup}
                    lastOfGroup
                    delivery={
                      send.status === "sending"
                        ? {
                            state: "sending",
                            label:
                              send.kind === "media"
                                ? t("delivery.uploading")
                                : t("delivery.sending"),
                          }
                        : { state: "failed", label: t("delivery.failed") }
                    }
                    failure={
                      send.status === "failed"
                        ? {
                            reason: errors(send.errorCode),
                            actions: (
                              <>
                                {send.retryable ? (
                                  <Button
                                    variant="secondary"
                                    onClick={() => onRetry(send)}
                                    data-retry=""
                                  >
                                    {t("retrySend")}
                                  </Button>
                                ) : null}
                                <Button
                                  variant="ghost"
                                  onClick={() => runtime.queue.discard(send.clientId)}
                                >
                                  {t("discard")}
                                </Button>
                              </>
                            ),
                          }
                        : null
                    }
                  >
                    {send.kind === "media" && send.fileName ? <bdi>{send.fileName} </bdi> : null}
                    {send.text}
                  </MessageBubble>,
                );
              }
              if (index === seenIndex && conversation.read) {
                const by = conversation.read.by;
                nodes.push(
                  <ThreadNote key={`seen-${item.key}`} kind="seen">
                    {t.rich("seenBy", {
                      name: () => (
                        <bdi>
                          {by.user_id && by.user_id === profile?.id
                            ? t("seenByYou")
                            : by.display_name || t("assignee.someone")}
                        </bdi>
                      ),
                      time: clockTime(conversation.read.at, locale),
                    })}
                  </ThreadNote>,
                );
              }
              return nodes;
            })}
            <li ref={bottom} aria-hidden="true" />
          </ol>
        )}
      </div>
      {newCount > 0 && !atBottom ? (
        <NewMessagesPill
          label={t("newMessages", { count: newCount })}
          onClick={() => {
            box.current?.scrollTo({ top: box.current.scrollHeight });
            setNewCount(0);
          }}
        />
      ) : null}
      <p className="sr-only" role="status" aria-live="polite">
        {newCount > 0 && !atBottom ? t("newMessageFrom", { name: customer }) : ""}
      </p>
      <LinkConfirm
        url={link}
        title={t("link.title")}
        description={t("link.description")}
        openLabel={t("link.open")}
        cancelLabel={t("link.cancel")}
        closeLabel={t("close")}
        onOpen={(url) => openConfirmed(url)}
        onClose={() => setLink(null)}
      />
      <MediaViewer
        open={viewer !== null}
        src={viewer ? (images.current.get(viewer) ?? null) : null}
        alt={t("media.imageFrom", { name: customer })}
        title={t("media.viewerTitle")}
        closeLabel={t("close")}
        downloadLabel={t("media.download")}
        onDownload={() => {
          const src = viewer ? images.current.get(viewer) : null;
          if (!src) return;
          const anchor = document.createElement("a");
          anchor.href = src;
          anchor.download = t("media.defaultImageName");
          document.body.append(anchor);
          anchor.click();
          anchor.remove();
        }}
        onClose={() => setViewer(null)}
        onPrevious={viewerIndex > 0 ? () => setViewer(imageIds[viewerIndex - 1]!) : null}
        onNext={
          viewerIndex >= 0 && viewerIndex < imageIds.length - 1
            ? () => setViewer(imageIds[viewerIndex + 1]!)
            : null
        }
        previousLabel={t("media.previous")}
        nextLabel={t("media.next")}
      />
    </div>
  );
}
