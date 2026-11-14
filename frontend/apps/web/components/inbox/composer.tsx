"use client";

/**
 * The composer (CP-01 .. CP-06, IL-07). Text, a file or - when the reply window has closed - an
 * approved template; one Idempotency-Key per bubble, reused on every retry; sends of one
 * conversation in order; the draft per conversation in memory and sessionStorage.
 */
import { useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { FileText, Paperclip, SendHorizontal } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import {
  AttachmentChip,
  Button,
  Composer as ComposerFrame,
  Dialog,
  IconButton,
  Input,
  Textarea,
} from "@wasla/ui";

import {
  composerMode,
  counterVisible,
  enterSends,
  isTouchFirst,
  readDraft,
  replyAssigns,
  writeDraft,
} from "../../lib/inbox/composer";
import { draftStorage } from "../../lib/inbox/draft-storage";
import { conversationKey, messagesKey, templatesKey } from "../../lib/inbox/keys";
import { checkFileToSend, SEND_TYPES } from "../../lib/inbox/media";
import { isRetryable, type PendingSend } from "../../lib/inbox/send-queue";
import { durationParts } from "../../lib/inbox/time";
import { appNow } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { useCodeText } from "./error-text";
import { useInboxRuntime } from "./inbox-runtime";
import type { ConversationRead, MessageRead } from "./people";

type Template = components["schemas"]["TemplateRead"];
type Page = { items: MessageRead[]; next_cursor: string | null };

function failureOf(error: unknown): { code: string; status: number | null } {
  if (error instanceof ApiError)
    return { code: error.code, status: error.status === 0 ? null : error.status };
  return { code: "network_error", status: null };
}

/** Sends a pending bubble with its own key; a retry calls this again with the same bubble. */
export function useSender(conversation: ConversationRead) {
  const { session } = useSession();
  const { scope } = useWorkspace();
  const runtime = useInboxRuntime();
  const queryClient = useQueryClient();
  const files = useRef(new Map<string, { file: File; caption: string }>());
  const templates = useRef(
    new Map<string, { name: string; language: string; components: unknown[] }>(),
  );

  const send = useCallback(
    (pending: PendingSend) =>
      runtime.queue.enqueue(conversation.id, async () => {
        const path = { conversation_id: conversation.id };
        const headers = { "Idempotency-Key": pending.idempotencyKey };
        try {
          let message: MessageRead;
          if (pending.kind === "text") {
            message = await unwrap(
              session.api.POST("/api/v1/conversations/{conversation_id}/messages", {
                params: { path, header: headers },
                body: { body: pending.text, preview_url: false },
              }),
            );
          } else if (pending.kind === "template") {
            const chosen = templates.current.get(pending.clientId)!;
            message = await unwrap(
              session.api.POST("/api/v1/conversations/{conversation_id}/messages/template", {
                params: { path, header: headers },
                body: {
                  name: chosen.name,
                  language: chosen.language,
                  components: chosen.components as Record<string, unknown>[],
                },
              }),
            );
          } else {
            const chosen = files.current.get(pending.clientId)!;
            const form = new FormData();
            form.append("file", chosen.file, chosen.file.name);
            if (chosen.caption) form.append("caption", chosen.caption);
            message = await unwrap(
              session.api.POST("/api/v1/conversations/{conversation_id}/messages/media", {
                params: { path, header: headers },
                body: form as never,
                bodySerializer: (body: unknown) => body as FormData,
              }),
            );
          }
          // The bubble becomes the server's message; a frame or a refetch with this id draws nothing new.
          queryClient.setQueryData<InfiniteData<Page, string | null>>(
            messagesKey(scope, conversation.id),
            (data) => {
              if (!data) return data;
              if (data.pages.some((page) => page.items.some((item) => item.id === message.id)))
                return data;
              const [first, ...rest] = data.pages;
              return {
                ...data,
                pages: [{ ...first!, items: [message, ...first!.items] }, ...rest],
              };
            },
          );
          runtime.queue.settle(pending.clientId, message.id);
          files.current.delete(pending.clientId);
          templates.current.delete(pending.clientId);
          void queryClient.invalidateQueries({ queryKey: conversationKey(scope, conversation.id) });
        } catch (error) {
          const failure = failureOf(error);
          if (failure.code === "idempotency_key_reused") {
            // A programming error, not the person's: a fresh key next time, nothing said beyond the retry.
            runtime.queue.fail(pending.clientId, { code: "fallback", retryable: false });
            return;
          }
          if (failure.code === "reply_window_closed" || failure.code === "conversation_closed") {
            void queryClient.invalidateQueries({
              queryKey: conversationKey(scope, conversation.id),
            });
          }
          runtime.queue.fail(pending.clientId, {
            code: failure.code,
            retryable: isRetryable(failure.status, failure.code),
          });
        }
      }),
    [conversation.id, runtime.queue, session, queryClient, scope],
  );

  const retry = useCallback(
    (pending: PendingSend) => {
      const again = runtime.queue.retry(pending.clientId);
      if (again) void send(again);
    },
    [runtime.queue, send],
  );

  return { send, retry, files: files.current, templates: templates.current };
}

export function ConversationComposer({
  conversation,
  optedOut,
  onReopen,
  sender,
}: {
  conversation: ConversationRead;
  optedOut: boolean;
  onReopen: () => void;
  sender: ReturnType<typeof useSender>;
}) {
  const t = useTranslations("app.inbox.composer");
  const errors = useCodeText();
  const format = useFormatter();
  const { workspaceId } = useWorkspace();
  const runtime = useInboxRuntime();
  const fieldId = useId();
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [text, setText] = useState(() => readDraft(draftStorage(), workspaceId, conversation.id));
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [drop, setDrop] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [now, setNow] = useState(() => appNow());
  const touch = typeof window !== "undefined" && isTouchFirst((query) => window.matchMedia(query));

  // The draft follows the text; a reload of this tab keeps it (CP-01).
  useEffect(
    () => writeDraft(draftStorage(), workspaceId, conversation.id, text),
    [text, workspaceId, conversation.id],
  );
  useEffect(() => {
    const timer = setInterval(() => setNow(appNow()), 60_000);
    return () => clearInterval(timer);
  }, []);
  // A failed bubble's words go back to the draft before the page goes (CP-02).
  useEffect(() => {
    const onHide = () => {
      const failed = runtime.queue
        .forConversation(conversation.id)
        .filter((send) => send.kind === "text" && send.status === "failed")
        .map((send) => send.text);
      if (failed.length)
        writeDraft(
          draftStorage(),
          workspaceId,
          conversation.id,
          [...failed, text].filter(Boolean).join("\n"),
        );
    };
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
  }, [runtime.queue, conversation.id, workspaceId, text]);

  const mode = composerMode(conversation, { now, optedOut });
  const limit = conversation.reply_policy.text_limit || 4096;
  const tooLong = [...text].length > limit;

  const pickFile = (chosen: File | null) => {
    setFileError(null);
    if (!chosen) return;
    const check = checkFileToSend(chosen);
    if (!check.ok) {
      setFileError(t(`fileRefused.${check.reason}`));
      return;
    }
    setFile(chosen);
  };

  const submit = () => {
    if (mode.kind !== "free") return;
    const body = text.trim();
    if (file) {
      const pending = runtime.queue.add({
        conversationId: conversation.id,
        kind: "media",
        text: body,
        fileName: file.name,
      });
      sender.files.set(pending.clientId, { file, caption: body });
      void sender.send(pending);
      setFile(null);
    } else {
      if (!body || tooLong) return;
      const pending = runtime.queue.add({
        conversationId: conversation.id,
        kind: "text",
        text: body,
      });
      void sender.send(pending);
    }
    setText("");
    writeDraft(draftStorage(), workspaceId, conversation.id, "");
  };

  let stopped: ReactNode = null;
  if (mode.kind === "closed") {
    stopped = (
      <>
        <p className="m-0 text-sm text-text">{t("closed")}</p>
        <Button variant="primary" onClick={onReopen}>
          {t("reopen")}
        </Button>
      </>
    );
  } else if (mode.kind === "opted-out") {
    stopped = <p className="m-0 text-sm text-text">{t("optedOut")}</p>;
  } else if (mode.kind === "unavailable") {
    stopped = <p className="m-0 text-sm text-text">{t("unavailable")}</p>;
  } else if (mode.kind === "templates-only") {
    stopped = (
      <>
        <p className="m-0 text-sm text-text">{t("windowClosed")}</p>
        <Button variant="primary" onClick={() => setTemplatesOpen(true)} data-choose-template="">
          {t("chooseTemplate")}
        </Button>
      </>
    );
  }

  const left =
    mode.kind === "free" && mode.expiresInMs !== null ? durationParts(mode.expiresInMs) : null;
  const notice = (
    <>
      {replyAssigns(conversation) && mode.kind === "free" ? (
        <p className="m-0">{t("assignsYou")}</p>
      ) : null}
      {left ? (
        <p className="m-0">{t("windowLeft", { hours: left.hours, minutes: left.minutes })}</p>
      ) : null}
      {conversation.mode === "ai" && mode.kind === "free" ? (
        <p className="m-0">{t("aiMode")}</p>
      ) : null}
    </>
  );

  return (
    <div
      onDragOver={(event) => {
        if (mode.kind !== "free") return;
        event.preventDefault();
        setDrop(true);
      }}
      onDragLeave={() => setDrop(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDrop(false);
        if (mode.kind === "free") pickFile(event.dataTransfer.files[0] ?? null);
      }}
    >
      <ComposerFrame
        dropActive={drop}
        dropLabel={t("drop")}
        notice={notice}
        stopped={stopped}
        attachment={
          file ? (
            <AttachmentChip
              name={file.name}
              size={`${format.number(file.size / 1048576, { maximumFractionDigits: 1, numberingSystem: "latn" })} ${t("mb")}`}
              removeLabel={t("removeFile")}
              onRemove={() => setFile(null)}
            />
          ) : null
        }
        field={
          <>
            <label htmlFor={fieldId} className="sr-only">
              {file ? t("captionLabel") : t("label")}
            </label>
            <Textarea
              id={fieldId}
              rows={2}
              value={text}
              data-composer-field=""
              className="max-h-media resize-none"
              onChange={(event) => setText(event.target.value)}
              onPaste={(event) => {
                const pasted = event.clipboardData.files[0];
                if (pasted) {
                  event.preventDefault();
                  pickFile(pasted);
                }
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                const native = event.nativeEvent;
                if (
                  enterSends(
                    {
                      key: event.key,
                      shiftKey: event.shiftKey,
                      altKey: event.altKey,
                      ctrlKey: event.ctrlKey,
                      metaKey: event.metaKey,
                      repeat: event.repeat,
                      isComposing: native.isComposing,
                      keyCode: native.keyCode,
                    },
                    touch,
                  )
                ) {
                  event.preventDefault();
                  submit();
                }
              }}
              aria-invalid={tooLong || undefined}
              aria-describedby={tooLong ? `${fieldId}-error` : undefined}
            />
          </>
        }
        error={
          fileError ??
          (tooLong ? <span id={`${fieldId}-error`}>{t("tooLong", { limit })}</span> : null)
        }
        counter={counterVisible([...text].length, limit) ? `${[...text].length} / ${limit}` : null}
        tools={
          <>
            <input
              ref={fileInput}
              type="file"
              hidden
              accept={SEND_TYPES.join(",")}
              onChange={(event) => {
                pickFile(event.target.files?.[0] ?? null);
                event.target.value = "";
              }}
            />
            <IconButton
              icon={Paperclip}
              iconName="Paperclip"
              label={t("attach")}
              onClick={() => fileInput.current?.click()}
            />
            {conversation.reply_policy.templates ? (
              <Button variant="ghost" onClick={() => setTemplatesOpen(true)}>
                <FileText aria-hidden="true" size={16} />
                {t("template")}
              </Button>
            ) : null}
          </>
        }
        primary={
          <Button
            variant="primary"
            onClick={submit}
            disabled={(!text.trim() && !file) || tooLong}
            data-send=""
          >
            <SendHorizontal aria-hidden="true" size={16} className="wasla-flip-rtl" />
            {t("send")}
          </Button>
        }
      />
      <TemplateDialog
        open={templatesOpen}
        onOpenChange={setTemplatesOpen}
        onSend={(chosen) => {
          const pending = runtime.queue.add({
            conversationId: conversation.id,
            kind: "template",
            text: chosen.preview,
          });
          sender.templates.set(pending.clientId, chosen);
          void sender.send(pending);
          setTemplatesOpen(false);
        }}
        errorsFallback={errors(null)}
      />
    </div>
  );
}

/** The template picker (CP-05): approved templates only, their parameters, a plain-text preview. */
function TemplateDialog({
  open,
  onOpenChange,
  onSend,
  errorsFallback,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSend: (chosen: {
    name: string;
    language: string;
    components: unknown[];
    preview: string;
  }) => void;
  errorsFallback: string;
}) {
  const t = useTranslations("app.inbox.templates");
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  const filterId = useId();
  const locale = useLocale();
  const [filter, setFilter] = useState("");
  const [chosen, setChosen] = useState<Template | null>(null);
  const [values, setValues] = useState<string[]>([]);
  const list = useQuery({
    queryKey: templatesKey(scope),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/templates", { params: { query: { status: "approved" } } })),
    enabled: open,
    staleTime: 5 * 60_000,
  });
  const usable = (list.data?.templates ?? []).filter(
    (template) =>
      template.status === "approved" && (template.category !== "marketing" || role !== "member"),
  );
  const shown = usable.filter((template) =>
    template.name.toLocaleLowerCase(locale).includes(filter.trim().toLocaleLowerCase(locale)),
  );
  const count = chosen?.variable_count ?? 0;
  const needsMore = (chosen?.components ?? []).some((component) => {
    const type = (component as { type?: unknown }).type;
    return (
      typeof type === "string" && ["header", "buttons"].includes(type.toLocaleLowerCase("en-US"))
    );
  });
  const preview = (chosen?.body_text ?? "").replace(
    /\{\{(\d+)\}\}/g,
    (_, index: string) => values[Number(index) - 1] || `{{${index}}}`,
  );
  const complete =
    chosen !== null &&
    values.slice(0, count).every((value) => value.trim()) &&
    values.length >= count;

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setChosen(null);
      setValues([]);
      setFilter("");
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={chosen ? chosen.name : t("title")}
      closeLabel={t("close")}
      footer={
        chosen ? (
          <>
            <Button variant="secondary" onClick={() => setChosen(null)}>
              {t("back")}
            </Button>
            <Button
              variant="primary"
              disabled={!complete || needsMore}
              onClick={() =>
                onSend({
                  name: chosen.name,
                  language: chosen.language,
                  components: count
                    ? [
                        {
                          type: "body",
                          parameters: values
                            .slice(0, count)
                            .map((value) => ({ type: "text", text: value.trim() })),
                        },
                      ]
                    : [],
                  preview,
                })
              }
              data-send-template=""
            >
              {t("send")}
            </Button>
          </>
        ) : undefined
      }
    >
      {chosen ? (
        <div className="flex flex-col gap-4">
          {needsMore ? <p className="m-0 text-sm text-text">{t("notInInbox")}</p> : null}
          {Array.from({ length: count }, (_, index) => (
            <div key={index} className="flex flex-col gap-1">
              <label htmlFor={`${filterId}-${index}`} className="text-sm font-medium text-text">
                {t("parameter", { number: index + 1 })}
              </label>
              <Input
                id={`${filterId}-${index}`}
                value={values[index] ?? ""}
                required
                onChange={(event) =>
                  setValues((current) => {
                    const next = [...current];
                    next[index] = event.target.value;
                    return next;
                  })
                }
              />
            </div>
          ))}
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium text-text">{t("preview")}</span>
            <p
              dir="auto"
              className="m-0 whitespace-pre-wrap rounded-md bg-secondary-subtle px-3 py-2 text-sm text-text"
              data-template-preview=""
            >
              {preview}
            </p>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <label htmlFor={filterId} className="text-sm font-medium text-text">
            {t("filter")}
          </label>
          <Input
            id={filterId}
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          {list.isPending ? (
            <p role="status" className="m-0 text-sm text-text-muted">
              {t("loading")}
            </p>
          ) : list.isError ? (
            <p className="m-0 text-sm text-text">
              {list.error instanceof ApiError ? errorsFallback : errorsFallback}
            </p>
          ) : shown.length === 0 ? (
            <p className="m-0 text-sm text-text">{usable.length ? t("noMatch") : t("none")}</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label={t("listLabel")}>
              {shown.map((template) => (
                <li key={template.id}>
                  <Button
                    variant="ghost"
                    className="w-full justify-start"
                    onClick={() => {
                      setChosen(template);
                      setValues(Array.from({ length: template.variable_count }, () => ""));
                    }}
                  >
                    <bdi>{template.name}</bdi>
                    <span className="text-xs text-text-muted">
                      {" · "}
                      <bdi>{template.language}</bdi> · {t(`category.${template.category}`)}
                    </span>
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Dialog>
  );
}
