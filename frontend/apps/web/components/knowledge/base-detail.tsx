"use client";

/**
 * APP-KB-02 (KD-01 .. KD-05, PO-01 .. PO-03): one knowledge base and its documents. The list reads
 * GET /knowledge/documents?knowledge_base_id= (a CursorPage) with the status filter in the URL and
 * the title search in memory only. A document's status is followed by polling the list - one loop,
 * 3 s backing off to 15 s, paused while the tab is hidden, stopped once every loaded row is ready or
 * failed (lib/knowledge/poll.ts; FEB-11 is deferred). An upload is checked against the server's own
 * limits (FEB-38) before anything is sent; a refused file is never sent. Nothing is optimistic.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type ReactNode,
} from "react";

import { ApiError, queryKey, unwrap, type components } from "@wasla/api-client";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  DocumentRow,
  DocumentStatusBadge,
  EmptyState,
  ErrorState,
  FileField,
  FilterBar,
  FormField,
  InlineEditField,
  Input,
  KeyValueList,
  RecordHeader,
  RecordList,
  Select,
  SidePanel,
  TextCounter,
  Textarea,
} from "@wasla/ui";

import { NEXT_PAGE_KEEPS_REFETCH } from "../../lib/inbox/live";
import {
  basesKey,
  baseKey,
  documentKey,
  DOCUMENTS,
  limitsKey,
  READY_DOCUMENT,
  similarKey,
} from "../../lib/knowledge/keys";
import {
  isTerminal,
  needsPolling,
  newlySettled,
  nextInterval,
  reindexing,
  statusFingerprint,
} from "../../lib/knowledge/poll";
import {
  acceptedExtensions,
  preCheck,
  textProblem,
  titleFromFile,
  type KnowledgeLimits,
} from "../../lib/knowledge/upload";
import {
  DOCUMENT_STATUSES,
  knowledgeBaseHref,
  parseDocumentsView,
  type DocumentsView,
} from "../../lib/knowledge/view";
import { useWorkspace } from "../app/workspace-context";
import {
  codeOf,
  isNotFound,
  useCounter,
  useIsAdmin,
  useNumber,
  useTimes,
} from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { NotForRole } from "./knowledge-home";

type DocumentRead = components["schemas"]["DocumentRead"];

const PAGE_SIZE = 25;
const SEARCH_DEBOUNCE_MS = 300;
const ALL = "all";

/** The panel's breakpoint (DESIGN.md's lg, FE-4's LY-01). */
const LG = 1024;

function useWidth(): number {
  return useSyncExternalStore(
    (notify) => {
      window.addEventListener("resize", notify);
      return () => window.removeEventListener("resize", notify);
    },
    () => window.innerWidth,
    () => 1280,
  );
}

/** Sizes in the page's locale: kilobytes under a megabyte, megabytes above (UI6-04). */
function useBytes(): (bytes: number) => string {
  const locale = useLocale();
  return (bytes) => {
    const megabytes = bytes / (1000 * 1000);
    return megabytes >= 1
      ? new Intl.NumberFormat(locale, {
          style: "unit",
          unit: "megabyte",
          maximumFractionDigits: 1,
          numberingSystem: "latn",
        }).format(megabytes)
      : new Intl.NumberFormat(locale, {
          style: "unit",
          unit: "kilobyte",
          maximumFractionDigits: 0,
          numberingSystem: "latn",
        }).format(Math.max(1, Math.round(bytes / 1000)));
  };
}

function useLimits() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useQuery({
    queryKey: limitsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/knowledge/limits")),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

function documentsPrefix(scope: { workspaceId: string; sessionGeneration: number }) {
  return [scope.workspaceId, scope.sessionGeneration, DOCUMENTS] as const;
}

/** After an upload, a replace, a retry or a delete: the lists and home's ready read are stale. */
function useDocumentsChanged() {
  const client = useQueryClient();
  const { scope } = useWorkspace();
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: documentsPrefix(scope) }),
      client.invalidateQueries({
        queryKey: [scope.workspaceId, scope.sessionGeneration, READY_DOCUMENT],
      }),
      client.invalidateQueries({
        queryKey: [scope.workspaceId, scope.sessionGeneration, "agents"],
      }),
    ]);
}

/* ------------------------------------------------------------------ upload */

interface Duplicate {
  kind: "exists" | "otherBase" | "sameContent";
  documentId: string | null;
}

function duplicateOf(error: unknown): Duplicate | null {
  if (!(error instanceof ApiError)) return null;
  const details = (error.details ?? {}) as { document_id?: string };
  const id = typeof details.document_id === "string" ? details.document_id : null;
  if (error.code === "document_source_exists") return { kind: "exists", documentId: id };
  if (error.code === "document_exists_in_other_knowledge_base")
    return { kind: "otherBase", documentId: id };
  if (error.code === "document_content_exists") return { kind: "sameContent", documentId: id };
  return null;
}

function UploadDialog({
  baseId,
  open,
  onOpenChange,
  onOpenDocument,
  replacing,
}: {
  baseId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpenDocument: (id: string) => void;
  /** Replace this document's content instead of adding one. */
  replacing?: DocumentRead | null;
}) {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const bytes = useBytes();
  const { session } = useSession();
  const limits = useLimits();
  const changed = useDocumentsChanged();
  const formId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const limit = limits.data as KnowledgeLimits | undefined;
  const check = file && limit ? preCheck(file, limit) : null;
  const reset = () => {
    setFile(null);
    setTitle("");
    setValidFrom("");
    setValidUntil("");
  };

  const send = useMutation({
    mutationFn: (replace: boolean) => {
      const form = new FormData();
      form.append("file", file!, file!.name);
      if (title.trim()) form.append("title", title.trim());
      if (check?.ok) form.append("source", check.source);
      if (!replacing) {
        if (validFrom) form.append("valid_from", new Date(validFrom).toISOString());
        if (validUntil) form.append("valid_until", new Date(validUntil).toISOString());
        return unwrap(
          session.api.POST("/api/v1/knowledge/bases/{knowledge_base_id}/documents/upload", {
            params: {
              path: { knowledge_base_id: baseId },
              query: replace ? { replace: true } : {},
            },
            body: form as never,
            bodySerializer: (body: unknown) => body as FormData,
          }),
        );
      }
      return unwrap(
        session.api.PUT("/api/v1/knowledge/documents/{document_id}/content/upload", {
          params: { path: { document_id: replacing.id } },
          body: form as never,
          bodySerializer: (body: unknown) => body as FormData,
        }),
      );
    },
    onSuccess: async () => {
      await changed();
      reset();
      onOpenChange(false);
    },
  });
  const duplicate = duplicateOf(send.error);
  const code = codeOf(send.error);
  const datesWrong = Boolean(
    validFrom && validUntil && new Date(validUntil) <= new Date(validFrom),
  );

  let problem: ReactNode = null;
  if (check && !check.ok && limit) {
    if (check.reason === "type")
      problem = t("upload.typeRefused", { extensions: check.extensions.join(" ") });
    else if (check.reason === "empty") problem = t("upload.emptyRefused");
    else
      problem = t("upload.sizeRefused", {
        size: bytes(file!.size),
        kind: t(`documents.type.${check.source}` as "documents.type.pdf"),
        max: bytes(check.maxBytes),
      });
  }
  const binaryMax = limit?.sources.find((row) => row.source === "pdf")?.max_bytes ?? 0;
  const textMax = limit?.sources.find((row) => row.source === "text")?.max_bytes ?? 0;
  const ready = Boolean(file && check?.ok && (replacing || title.trim()) && !datesWrong && limit);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    // KD-03: nothing is sent that the server would refuse for its type or size.
    if (!ready || send.isPending) return;
    send.mutate(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          reset();
          send.reset();
        }
        onOpenChange(next);
      }}
      title={replacing ? t("panel.replace") : t("upload.title")}
      closeLabel={t("panel.close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("edit.cancel")}
          </Button>
          <Button type="submit" form={formId} busy={send.isPending} disabled={!ready}>
            {send.isPending ? t("upload.sending") : t("upload.submit")}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        className="flex flex-col gap-4"
        onSubmit={submit}
        data-upload-form=""
      >
        <FileField
          label={t("upload.file")}
          chooseLabel={t("upload.choose")}
          accept={limit ? acceptedExtensions(limit).join(",") : ""}
          chosen={file ? t("upload.chosen", { name: file.name, size: bytes(file.size) }) : null}
          hint={
            limit ? t("upload.hint", { binary: bytes(binaryMax), text: bytes(textMax) }) : undefined
          }
          problem={problem}
          disabled={!limit || send.isPending}
          onFile={(next) => {
            send.reset();
            setFile(next);
            if (next && limit && !replacing)
              setTitle(titleFromFile(next.name, limit.max_title_length));
          }}
        />
        {replacing ? null : (
          <>
            <FormField label={t("upload.titleField")} required>
              {(control) => (
                <Input
                  {...control}
                  dir="auto"
                  autoComplete="off"
                  maxLength={limit?.max_title_length}
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  data-field="document-title"
                />
              )}
            </FormField>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FormField label={t("upload.validFrom")}>
                {(control) => (
                  <Input
                    {...control}
                    type="date"
                    value={validFrom}
                    onChange={(event) => setValidFrom(event.target.value)}
                  />
                )}
              </FormField>
              <FormField
                label={t("upload.validUntil")}
                error={datesWrong ? t("upload.dates") : undefined}
              >
                {(control) => (
                  <Input
                    {...control}
                    type="date"
                    value={validUntil}
                    onChange={(event) => setValidUntil(event.target.value)}
                  />
                )}
              </FormField>
            </div>
          </>
        )}
        {duplicate ? (
          <div
            role="alert"
            className="flex flex-col items-start gap-2"
            data-duplicate={duplicate.kind}
          >
            <p className="m-0 text-sm font-medium text-text">
              {duplicate.kind === "exists"
                ? t("upload.exists")
                : duplicate.kind === "otherBase"
                  ? t("upload.otherBase")
                  : t("upload.sameContent")}
            </p>
            {duplicate.kind === "exists" ? (
              <Button
                variant="secondary"
                busy={send.isPending}
                onClick={() => send.mutate(true)}
                data-action="replace-existing"
              >
                {t("upload.replaceIt")}
              </Button>
            ) : duplicate.kind === "sameContent" && duplicate.documentId ? (
              <Button
                variant="secondary"
                onClick={() => {
                  onOpenChange(false);
                  onOpenDocument(duplicate.documentId!);
                }}
              >
                {t("upload.openIt")}
              </Button>
            ) : null}
          </div>
        ) : send.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(code)}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

function TextDialog({
  baseId,
  open,
  onOpenChange,
  replacing,
}: {
  baseId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  replacing?: DocumentRead | null;
}) {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const counter = useCounter();
  const { session } = useSession();
  const limits = useLimits();
  const changed = useDocumentsChanged();
  const formId = useId();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const max = limits.data?.max_characters ?? 0;
  const problem = limits.data ? textProblem(content, limits.data) : "required";
  const send = useMutation({
    mutationFn: () =>
      replacing
        ? unwrap(
            session.api.PUT("/api/v1/knowledge/documents/{document_id}/content", {
              params: { path: { document_id: replacing.id } },
              body: { content, source: "text" },
            }),
          )
        : unwrap(
            session.api.POST("/api/v1/knowledge/bases/{knowledge_base_id}/documents", {
              params: { path: { knowledge_base_id: baseId } },
              body: { title: title.trim(), content, source: "text" },
            }),
          ),
    onSuccess: async () => {
      await changed();
      setTitle("");
      setContent("");
      onOpenChange(false);
    },
  });
  const ready = !problem && (replacing || title.trim());
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) send.reset();
        onOpenChange(next);
      }}
      title={replacing ? t("panel.replace") : t("text.title")}
      closeLabel={t("panel.close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("edit.cancel")}
          </Button>
          <Button type="submit" form={formId} busy={send.isPending} disabled={!ready}>
            {t("text.submit")}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (ready && !send.isPending) send.mutate();
        }}
      >
        {replacing ? null : (
          <FormField label={t("text.titleField")} required>
            {(control) => (
              <Input
                {...control}
                dir="auto"
                autoComplete="off"
                maxLength={limits.data?.max_title_length}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                data-field="text-title"
              />
            )}
          </FormField>
        )}
        <FormField label={t("text.content")} hint={t("text.contentHint")} required>
          {(control) => (
            <Textarea
              {...control}
              dir="auto"
              rows={10}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              data-field="text-content"
            />
          )}
        </FormField>
        {limits.data ? (
          <TextCounter length={content.length} max={max} text={counter(content.length, max)} />
        ) : null}
        {send.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {duplicateOf(send.error)?.kind === "exists"
              ? t("upload.exists")
              : duplicateOf(send.error)?.kind === "sameContent"
                ? t("upload.sameContent")
                : errors(codeOf(send.error))}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

export function BaseDetail({ id }: { id: string }) {
  const admin = useIsAdmin();
  return admin ? <BaseScreen id={id} /> : <NotForRole />;
}

function BaseScreen({ id }: { id: string }) {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const router = useRouter();
  const search = useSearchParams();
  const times = useTimes();
  const bytes = useBytes();
  const counter = useCounter();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const view = useMemo(() => parseDocumentsView(new URLSearchParams(search.toString())), [search]);
  const go = (next: DocumentsView) =>
    router.replace(knowledgeBaseHref(id, next), { scroll: false });
  const searchId = useId();

  const base = useQuery({
    queryKey: baseKey(scope, id),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/knowledge/bases/{knowledge_base_id}", {
          params: { path: { knowledge_base_id: id } },
        }),
      ),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });

  const [typed, setTyped] = useState("");
  const [term, setTerm] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setTerm(typed.trim().slice(0, 300)), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  // PO-02: the interval and the last fingerprint live across polls of this list.
  const poll = useRef<{ interval: number | null; fingerprint: string; rows: DocumentRead[] }>({
    interval: null,
    fingerprint: "",
    rows: [],
  });
  const [announcement, setAnnouncement] = useState("");
  const [refreshFailures, setRefreshFailures] = useState(0);

  const documents = useInfiniteQuery({
    queryKey: [
      ...queryKey(scope, DOCUMENTS, { knowledge_base_id: id, status: view.status }),
      term,
    ] as const,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/knowledge/documents", {
          params: {
            query: {
              knowledge_base_id: id,
              limit: PAGE_SIZE,
              ...(view.status ? { status: view.status } : {}),
              ...(term ? { title: term } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: base.isSuccess,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      const rows = query.state.data?.pages.flatMap((page) => page.items) ?? [];
      if (!needsPolling(rows)) {
        poll.current.interval = null;
        return false;
      }
      const fingerprint = statusFingerprint(rows);
      const changed = fingerprint !== poll.current.fingerprint;
      poll.current.fingerprint = fingerprint;
      poll.current.interval = nextInterval(poll.current.interval, changed);
      return poll.current.interval;
    },
  });
  const rows = useMemo(
    () => documents.data?.pages.flatMap((page) => page.items) ?? [],
    [documents.data],
  );

  // One polite announcement per poll that settled something (AJ-10), never one per row.
  useEffect(() => {
    const settled = newlySettled(poll.current.rows, rows);
    poll.current.rows = rows;
    const parts = [
      settled.ready ? t("documents.settledReady", { count: settled.ready }) : "",
      settled.failed ? t("documents.settledFailed", { count: settled.failed }) : "",
    ].filter(Boolean);
    if (parts.length) setAnnouncement(parts.join(" "));
  }, [rows, t]);
  useEffect(() => {
    setRefreshFailures((count) => (documents.isRefetchError ? count + 1 : 0));
  }, [documents.isRefetchError, documents.errorUpdatedAt, documents.dataUpdatedAt]);

  const [uploading, setUploading] = useState(false);
  const [writing, setWriting] = useState(false);
  const [deleting, setDeleting] = useState<"empty" | "full" | null>(null);
  const changedDocs = useDocumentsChanged();

  const rename = useMutation({
    mutationFn: (body: { name?: string; description?: string }) =>
      unwrap(
        session.api.PATCH("/api/v1/knowledge/bases/{knowledge_base_id}", {
          params: { path: { knowledge_base_id: id } },
          body,
        }),
      ),
    onSuccess: async (updated) => {
      client.setQueryData(baseKey(scope, id), updated);
      await client.invalidateQueries({ queryKey: basesKey(scope) });
    },
  });
  const remove = useMutation({
    mutationFn: (cascade: boolean) =>
      unwrap(
        session.api.DELETE("/api/v1/knowledge/bases/{knowledge_base_id}", {
          params: { path: { knowledge_base_id: id }, query: cascade ? { cascade: true } : {} },
        }),
      ),
    onSuccess: async () => {
      setDeleting(null);
      await Promise.all([client.invalidateQueries({ queryKey: basesKey(scope) }), changedDocs()]);
      router.push("/knowledge");
    },
    onError: (error) => {
      // KN-03: an empty base deletes at once; one with documents answers 409 and asks for its name.
      if (error instanceof ApiError && error.code === "knowledge_base_not_empty")
        setDeleting("full");
    },
  });

  if (base.isError) {
    return isNotFound(base.error) ||
      (base.error instanceof ApiError && base.error.status === 422) ? (
      <ErrorState
        headingLevel={1}
        title={t("notFound")}
        action={
          <NextLink href="/knowledge" className="text-primary underline underline-offset-4">
            {t("backToList")}
          </NextLink>
        }
      />
    ) : (
      <ErrorState
        headingLevel={1}
        title={errors(codeOf(base.error))}
        action={
          <Button variant="secondary" onClick={() => void base.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  }
  if (base.isPending) {
    return (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  }
  const data = base.data;
  const open = rows.find((row) => row.id === view.doc) ?? null;
  const loadedCount = rows.length;
  const more = documents.hasNextPage;

  const list = documents.isError ? (
    <ErrorState
      headingLevel={2}
      title={errors(codeOf(documents.error))}
      action={
        <Button variant="secondary" onClick={() => void documents.refetch()}>
          {t("retry")}
        </Button>
      }
    />
  ) : documents.isPending ? (
    <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
      {t("loading")}
    </p>
  ) : !rows.length ? (
    view.status || term ? (
      <EmptyState headingLevel={2} title={t("documents.noMatch")} />
    ) : (
      <EmptyState
        headingLevel={2}
        title={t("documents.empty")}
        action={
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => setUploading(true)}>{t("documents.upload")}</Button>
            <Button variant="secondary" onClick={() => setWriting(true)}>
              {t("documents.addText")}
            </Button>
          </div>
        }
      />
    )
  ) : (
    <RecordList
      label={t("documents.label")}
      busy={documents.isFetchingNextPage}
      footer={
        more ? (
          <Button
            variant="secondary"
            busy={documents.isFetchingNextPage}
            onClick={() => void documents.fetchNextPage(NEXT_PAGE_KEEPS_REFETCH)}
          >
            {t("loadMore")}
          </Button>
        ) : null
      }
    >
      {rows.map((row) => {
        const when = times(row.created_at);
        const status = t(`documents.status.${row.status}` as "documents.status.ready");
        const marks = [
          ...(row.needs_reindex ? [t("documents.needsReindex")] : []),
          ...(reindexing(row) ? [t("documents.reindexing")] : []),
        ];
        return (
          <DocumentRow
            key={row.id}
            title={row.title}
            status={<DocumentStatusBadge status={row.status} label={status} />}
            marks={marks}
            type={t(`documents.type.${row.source}` as "documents.type.pdf")}
            size={bytes(row.byte_size)}
            time={when?.time ?? null}
            timeFull={when?.full ?? null}
            selected={row.id === view.doc}
            onOpen={() => go({ ...view, doc: row.id })}
          />
        );
      })}
    </RecordList>
  );

  return (
    <div className="flex flex-col gap-6" data-screen="knowledge-base">
      <NextLink
        href="/knowledge"
        className="self-start text-sm text-primary underline underline-offset-4"
      >
        {t("backToList")}
      </NextLink>
      <RecordHeader
        title={data.name}
        titleId="base-title"
        sub={data.description ? <bdi dir="auto">{data.description}</bdi> : null}
        actions={
          <>
            <Button onClick={() => setUploading(true)} data-action="upload">
              {t("documents.upload")}
            </Button>
            <Button variant="secondary" onClick={() => setWriting(true)} data-action="add-text">
              {t("documents.addText")}
            </Button>
            <Button
              variant="ghost"
              busy={remove.isPending && deleting === null}
              onClick={() => setDeleting("empty")}
              data-action="delete-base"
            >
              {t("delete.action")}
            </Button>
          </>
        }
        facts={
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <InlineEditField
              label={t("edit.name")}
              value={data.name}
              editLabel={t("edit.editName")}
              saveLabel={t("edit.save")}
              cancelLabel={t("edit.cancel")}
              maxLength={200}
              counter={counter}
              error={codeOf(rename.error) ? errors(codeOf(rename.error)) : undefined}
              onSave={(value) => rename.mutateAsync({ name: value.trim() })}
              onReset={() => rename.reset()}
            />
            <InlineEditField
              label={t("edit.description")}
              value={data.description ?? ""}
              editLabel={t("edit.editDescription")}
              saveLabel={t("edit.save")}
              cancelLabel={t("edit.cancel")}
              maxLength={500}
              counter={counter}
              onSave={(value) => rename.mutateAsync({ description: value.trim() })}
              onReset={() => rename.reset()}
            />
          </div>
        }
      />

      <FilterBar label={t("documents.filter")}>
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex min-w-48 flex-col gap-1">
            <span id={`${searchId}-status`} className="text-sm font-medium text-text">
              {t("documents.filter")}
            </span>
            <Select
              aria-labelledby={`${searchId}-status`}
              value={view.status ?? ALL}
              onValueChange={(value) =>
                go({ status: value === ALL ? null : (value as DocumentsView["status"]), doc: null })
              }
              options={[
                { value: ALL, label: t("documents.allStatuses") },
                ...DOCUMENT_STATUSES.map((status) => ({
                  value: status,
                  label: t(`documents.status.${status}`),
                })),
              ]}
            />
          </div>
          <div className="flex min-w-60 flex-1 flex-col gap-1">
            <label htmlFor={searchId} className="text-sm font-medium text-text">
              {t("documents.search")}
            </label>
            <Input
              id={searchId}
              type="search"
              dir="auto"
              autoComplete="off"
              maxLength={300}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              aria-describedby={`${searchId}-hint`}
              data-documents-search=""
            />
            <p id={`${searchId}-hint`} className="m-0 text-sm text-text-muted">
              {t("documents.searchHint")}
            </p>
          </div>
        </div>
      </FilterBar>

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        <Card as="div" className="min-w-0 flex-1" data-documents-list="">
          {list}
          {refreshFailures >= 2 ? (
            <p className="m-0 mt-3 text-sm text-text-muted" data-refresh-failed="">
              {t("documents.refreshFailed")}
            </p>
          ) : null}
          <p role="status" aria-live="polite" className="sr-only" data-documents-live="">
            {announcement}
          </p>
        </Card>
        {view.doc ? (
          <DocumentDetails
            key={view.doc}
            baseId={id}
            documentId={view.doc}
            row={open}
            loadedCount={loadedCount}
            onClose={() => go({ ...view, doc: null })}
            onOpen={(doc) => go({ ...view, doc })}
          />
        ) : null}
      </div>

      <UploadDialog
        baseId={id}
        open={uploading}
        onOpenChange={setUploading}
        onOpenDocument={(doc) => go({ ...view, doc })}
      />
      <TextDialog baseId={id} open={writing} onOpenChange={setWriting} />

      <Dialog
        open={deleting === "empty"}
        onOpenChange={(next) => !next && setDeleting(null)}
        title={t("delete.emptyTitle")}
        closeLabel={t("panel.close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleting(null)}>
              {t("edit.cancel")}
            </Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => remove.mutate(false)}
              data-action="confirm-delete-base"
            >
              {t("delete.confirm")}
            </Button>
          </>
        }
      >
        {remove.isError && codeOf(remove.error) !== "knowledge_base_not_empty" ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {codeOf(remove.error) === "knowledge_base_delete_incomplete"
              ? t("delete.incomplete")
              : errors(codeOf(remove.error))}
          </p>
        ) : null}
      </Dialog>
      <ConfirmDialog
        open={deleting === "full"}
        onOpenChange={(next) => !next && setDeleting(null)}
        title={t("delete.fullTitle")}
        description={
          <>
            {t("delete.fullBody")}{" "}
            {loadedCount
              ? more
                ? t("delete.countAtLeast", { count: loadedCount })
                : t("delete.count", { count: loadedCount })
              : null}
          </>
        }
        closeLabel={t("panel.close")}
        confirmationText={data.name}
        confirmationLabel={t("delete.typeName")}
        confirmLabel={t("delete.confirm")}
        cancelLabel={t("edit.cancel")}
        onConfirm={() => remove.mutate(true)}
      />
    </div>
  );
}

function DocumentDetails({
  baseId,
  documentId,
  row,
  onClose,
  onOpen,
}: {
  baseId: string;
  documentId: string;
  row: DocumentRead | null;
  loadedCount: number;
  onClose: () => void;
  onOpen: (id: string) => void;
}) {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const format = useFormatter();
  const bytes = useBytes();
  const number = useNumber();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const changed = useDocumentsChanged();
  const wide = useWidth() >= LG;
  // Escape closes the pane as it closes the drawer, and focus goes back to the row (KD-05).
  const pane = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!wide) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // A dialog open above the page owns Escape.
      if (document.querySelector('[role="dialog"]')) return;
      const row = document.querySelector<HTMLElement>(
        '[data-document-row] button[aria-current="true"]',
      );
      onClose();
      if (row) setTimeout(() => row.focus(), 0);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [wide, onClose]);
  const [replacing, setReplacing] = useState<"file" | "text" | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [similarOpen, setSimilarOpen] = useState(false);
  // PO-01: while the list polls (this row is loaded and not terminal), the panel draws the list's row
  // and does not poll itself; a document not in the loaded rows is read here.
  const own = useQuery({
    queryKey: documentKey(scope, documentId),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/knowledge/documents/{document_id}", {
          params: { path: { document_id: documentId } },
        }),
      ),
    enabled: row === null,
    refetchInterval: (query) => (query.state.data && !isTerminal(query.state.data) ? 5000 : false),
    refetchIntervalInBackground: false,
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const doc = row ?? own.data ?? null;
  const similar = useQuery({
    queryKey: similarKey(scope, documentId),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/knowledge/documents/{document_id}/similar", {
          params: { path: { document_id: documentId } },
        }),
      ),
    enabled: similarOpen,
  });
  const retry = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/knowledge/documents/{document_id}/ingest", {
          params: { path: { document_id: documentId } },
        }),
      ),
    onSettled: () => changed(),
  });
  const remove = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.DELETE("/api/v1/knowledge/documents/{document_id}", {
          params: { path: { document_id: documentId } },
        }),
      ),
    onSuccess: async () => {
      setConfirmDelete(false);
      onClose();
      await changed();
    },
  });

  const date = (iso: string | null | undefined) =>
    iso ? format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" }) : null;

  let body: ReactNode;
  if (!doc && own.isError) {
    body = (
      <p className="m-0 text-md text-text">
        {isNotFound(own.error) ? t("panel.notFound") : errors(codeOf(own.error))}
      </p>
    );
  } else if (!doc) {
    body = (
      <p role="status" className="m-0 text-sm text-text-muted">
        {t("loading")}
      </p>
    );
  } else {
    const status = t(`documents.status.${doc.status}` as "documents.status.ready");
    const warnings = Object.entries(doc.extraction_warnings ?? {});
    const canRetry = doc.status === "failed" || doc.needs_reindex;
    body = (
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-2">
          <DocumentStatusBadge status={doc.status} label={status} />
          {doc.needs_reindex ? (
            <span className="text-sm font-medium text-text">{t("documents.needsReindex")}</span>
          ) : null}
          {reindexing(doc) ? (
            <span className="text-sm font-medium text-text">{t("documents.reindexing")}</span>
          ) : null}
        </div>
        {doc.status === "failed" && doc.error ? (
          <Banner tone="danger" title={t("panel.error")} name="document-error">
            <span dir="auto">{doc.error}</span>
          </Banner>
        ) : null}
        {warnings.length ? (
          <div className="flex flex-col gap-1" data-document-warnings="">
            <span className="text-sm font-medium text-text">{t("panel.warnings")}</span>
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {warnings.map(([key, count]) => (
                <li key={key} className="text-sm text-text">
                  {key === "digits_glued_to_arabic" || key === "arabic_indic_leading_zero"
                    ? t(`panel.warning.${key}`, { count })
                    : t("panel.warning.other", { key, count: number(count) })}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <KeyValueList
          items={[
            {
              key: "type",
              label: t("panel.type"),
              value: t(`documents.type.${doc.source}` as "documents.type.pdf"),
            },
            ...(doc.filename
              ? [
                  {
                    key: "file",
                    label: t("panel.file"),
                    value: <bdi dir="auto">{doc.filename}</bdi>,
                  },
                ]
              : []),
            { key: "size", label: t("panel.size"), value: <bdi>{bytes(doc.byte_size)}</bdi> },
            ...(doc.status === "ready"
              ? [
                  {
                    key: "chunks",
                    label: t("panel.chunks"),
                    value: <bdi>{number(doc.chunk_count)}</bdi>,
                  },
                ]
              : []),
            { key: "added", label: t("panel.added"), value: date(doc.created_at) },
            ...(doc.ingested_at
              ? [{ key: "ingested", label: t("panel.ingested"), value: date(doc.ingested_at) }]
              : []),
            ...(doc.valid_from
              ? [{ key: "from", label: t("panel.validFrom"), value: date(doc.valid_from) }]
              : []),
            ...(doc.valid_until
              ? [{ key: "until", label: t("panel.validUntil"), value: date(doc.valid_until) }]
              : []),
            ...(doc.indexing
              ? [
                  {
                    key: "attempt",
                    label: t("panel.attempt"),
                    value: t("panel.attemptValue", { attempts: doc.indexing.attempts }),
                  },
                ]
              : []),
            ...(doc.supersedes_document_id
              ? [
                  {
                    key: "replaces",
                    label: t("panel.replaces"),
                    value: (
                      <button
                        type="button"
                        className="min-h-target cursor-pointer border-0 bg-transparent p-0 text-start text-primary underline underline-offset-4"
                        onClick={() => onOpen(doc.supersedes_document_id!)}
                      >
                        {t("upload.openIt")}
                      </button>
                    ),
                  },
                ]
              : []),
            {
              key: "original",
              label: t("panel.original"),
              value: doc.original_available ? t("panel.originalYes") : t("panel.originalNo"),
            },
          ]}
        />
        <div className="flex flex-wrap gap-3">
          <Button
            variant="secondary"
            onClick={() => setReplacing("file")}
            data-action="replace-file"
          >
            {t("panel.replace")} · {t("panel.replaceFile")}
          </Button>
          <Button
            variant="secondary"
            onClick={() => setReplacing("text")}
            data-action="replace-text"
          >
            {t("panel.replace")} · {t("panel.replaceText")}
          </Button>
          {canRetry ? (
            <Button
              variant="secondary"
              busy={retry.isPending}
              onClick={() => retry.mutate()}
              data-action="retry-indexing"
            >
              {t("panel.retry")}
            </Button>
          ) : null}
          <Button
            variant="danger"
            onClick={() => setConfirmDelete(true)}
            data-action="delete-document"
          >
            {t("panel.delete")}
          </Button>
        </div>
        {retry.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(codeOf(retry.error))}
          </p>
        ) : null}
        <div className="flex flex-col gap-2">
          <button
            type="button"
            aria-expanded={similarOpen}
            onClick={() => setSimilarOpen((value) => !value)}
            className="min-h-target cursor-pointer self-start border-0 bg-transparent p-0 text-start text-md font-semibold text-primary underline underline-offset-4"
          >
            {t("panel.similar")}
          </button>
          {similarOpen ? (
            similar.isPending ? (
              <p role="status" className="m-0 text-sm text-text-muted">
                {t("loading")}
              </p>
            ) : similar.isError ? (
              <p className="m-0 text-sm text-text">{errors(codeOf(similar.error))}</p>
            ) : similar.data.length ? (
              <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label={t("panel.similar")}>
                {similar.data.map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="min-h-target cursor-pointer border-0 bg-transparent p-0 text-start text-sm text-primary underline underline-offset-4"
                      onClick={() =>
                        item.knowledge_base_id === baseId ? onOpen(item.id) : undefined
                      }
                    >
                      <bdi dir="auto">{item.title}</bdi>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="m-0 text-sm text-text-muted">{t("panel.similarNone")}</p>
            )
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <>
      {/* LY6-01: a pane beside the list from 1024 px, a drawer below it (Back closes it: the id is
          in the URL). */}
      {wide ? (
        <div ref={pane} className="shrink-0" data-document-panel="">
          <SidePanel
            title={doc?.title ?? t("panel.label")}
            variant="pane"
            closeLabel={t("panel.close")}
            headingId="document-panel-heading"
          >
            <button
              type="button"
              className="self-start text-sm text-primary underline underline-offset-4"
              onClick={onClose}
            >
              {t("panel.close")}
            </button>
            {body}
          </SidePanel>
        </div>
      ) : (
        <SidePanel
          title={doc?.title ?? t("panel.label")}
          variant="drawer"
          open
          onOpenChange={(next) => !next && onClose()}
          closeLabel={t("panel.close")}
          headingId="document-panel-heading"
        >
          {body}
        </SidePanel>
      )}
      {doc ? (
        <>
          <UploadDialog
            baseId={baseId}
            open={replacing === "file"}
            onOpenChange={(next) => !next && setReplacing(null)}
            onOpenDocument={onOpen}
            replacing={doc}
          />
          <TextDialog
            baseId={baseId}
            open={replacing === "text"}
            onOpenChange={(next) => !next && setReplacing(null)}
            replacing={doc}
          />
        </>
      ) : null}
      <Dialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("panel.deleteTitle")}
        description={t("panel.deleteBody")}
        closeLabel={t("panel.close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmDelete(false)}>
              {t("edit.cancel")}
            </Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => remove.mutate()}
              data-action="confirm-delete-document"
            >
              {t("panel.deleteConfirm")}
            </Button>
          </>
        }
      >
        {remove.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(codeOf(remove.error))}
          </p>
        ) : null}
      </Dialog>
    </>
  );
}
