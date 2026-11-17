"use client";

/**
 * APP-KB-01 (KN-01 .. KN-06): the knowledge bases, the answer settings, "Test a question" and the
 * unanswered questions - for owners and administrators (OQ-3). A member reaching /knowledge sees
 * the role state and no knowledge request is sent. The settings, the test and the report read only
 * when their section is opened. A test question and the gap queries live in this component's memory
 * only: never in the URL, the history or any storage.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import {
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  FormField,
  GapList,
  Input,
  PageHeader,
  PassageList,
  RadioGroup,
  RecordList,
  SegmentedControl,
  Select,
  SettingSwitchRow,
  Switch,
  TextCounter,
  Textarea,
} from "@wasla/ui";

import { withHeaderCursor } from "../../lib/knowledge/bases";
import { basesKey, gapsKey, settingsKey } from "../../lib/knowledge/keys";
import { knowledgeBaseHref } from "../../lib/knowledge/view";
import { useWorkspace } from "../app/workspace-context";
import { codeOf, rowLink, useCounter, useIsAdmin, useNumber, useTimes } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";

type KnowledgeBase = components["schemas"]["KnowledgeBaseRead"];

export const BASES_PAGE_SIZE = 50;
const NAME_MAX = 200;
const DESCRIPTION_MAX = 500;
const QUESTION_MAX = 2000;
const SWITCHES = [
  "hybrid",
  "rerank",
  "confidence_tiers",
  "pre_retrieval",
  "parallel_sentiment",
] as const;
const GROUNDING = ["off", "observe", "correct", "strict"] as const;
const GAP_DAYS = [7, 30, 90] as const;

export function useBasesList(enabled = true) {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useInfiniteQuery({
    queryKey: basesKey(scope),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      withHeaderCursor<KnowledgeBase>(
        session.api.GET("/api/v1/knowledge/bases", {
          params: {
            query: { limit: BASES_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.nextCursor,
    enabled,
  });
}

/** The role state of an owner's screen reached by a member: words, a way home, no request. */
export function NotForRole() {
  const t = useTranslations("app.knowledge");
  return (
    <EmptyState
      headingLevel={1}
      title={t("notForRole")}
      action={
        <NextLink href="/" className="text-primary underline underline-offset-4">
          {t("backToList")}
        </NextLink>
      }
    />
  );
}

function Loading() {
  const t = useTranslations("app.knowledge");
  return (
    <p role="status" className="m-0 text-sm text-text-muted">
      {t("loading")}
    </p>
  );
}

/** A section that reads nothing until it is opened (KN-04 .. KN-06). */
function Disclosure({
  id,
  title,
  line,
  children,
}: {
  id: string;
  title: string;
  line: string;
  children: () => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Card as="section" aria-labelledby={`${id}-title`} data-disclosure={id}>
      <div className="flex flex-col gap-4">
        <h2 id={`${id}-title`} className="m-0 text-xl font-semibold text-text">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={`${id}-body`}
            onClick={() => setOpen((value) => !value)}
            className="flex min-h-target w-full cursor-pointer items-center justify-between gap-3 border-0 bg-transparent p-0 text-start text-xl font-semibold text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            {title}
            <span aria-hidden className="text-md text-text-muted">
              {open ? "−" : "+"}
            </span>
          </button>
        </h2>
        <p className="m-0 text-sm text-text-muted">{line}</p>
        <div id={`${id}-body`} hidden={!open}>
          {open ? children() : null}
        </div>
      </div>
    </Card>
  );
}

function CreateBaseDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const router = useRouter();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const formId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/knowledge/bases", {
          body: {
            name: name.trim(),
            ...(description.trim() ? { description: description.trim() } : {}),
          },
        }),
      ),
    onSuccess: async (base) => {
      await client.invalidateQueries({ queryKey: basesKey(scope) });
      onOpenChange(false);
      router.push(knowledgeBaseHref(base.id));
    },
  });
  const code = codeOf(create.error);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || name.trim().length > NAME_MAX || create.isPending) return;
    create.mutate();
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setName("");
          setDescription("");
          create.reset();
        }
        onOpenChange(next);
      }}
      title={t("create.title")}
      closeLabel={t("panel.close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("edit.cancel")}
          </Button>
          <Button type="submit" form={formId} busy={create.isPending} disabled={!name.trim()}>
            {t("create.submit")}
          </Button>
        </>
      }
    >
      <form id={formId} noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <FormField
          label={t("create.name")}
          required
          error={code === "knowledge_base_name_exists" ? errors(code) : undefined}
        >
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              maxLength={NAME_MAX}
              value={name}
              onChange={(event) => setName(event.target.value)}
              data-field="base-name"
            />
          )}
        </FormField>
        <FormField label={t("create.description")}>
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              maxLength={DESCRIPTION_MAX}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          )}
        </FormField>
        {create.isError && code !== "knowledge_base_name_exists" ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(code)}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

function AnswerSettings() {
  const t = useTranslations("app.knowledge.settings");
  const errors = useCodeText();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const settings = useQuery({
    queryKey: settingsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/knowledge/settings")),
  });
  const change = useMutation({
    mutationFn: (body: Record<string, boolean | string | null>) =>
      unwrap(session.api.PATCH("/api/v1/knowledge/settings", { body })),
    onSuccess: (data) => client.setQueryData(settingsKey(scope), data),
  });
  if (settings.isPending) return <Loading />;
  if (settings.isError)
    return <p className="m-0 text-sm text-text">{errors(codeOf(settings.error))}</p>;
  const { overrides, effective } = settings.data;
  const overridden = (key: string) => key in overrides && overrides[key] !== null;
  return (
    <div className="flex flex-col gap-3">
      <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label={t("title")}>
        {SWITCHES.map((key) => (
          <SettingSwitchRow
            key={key}
            label={t(`${key}.label`)}
            description={t(`${key}.description`)}
            sourceLabel={overridden(key) ? t("workspace") : t("default")}
            overridden={overridden(key)}
            useDefaultLabel={t("useDefault")}
            busy={change.isPending}
            onUseDefault={() => change.mutate({ [key]: null })}
            control={({ labelId, descriptionId }) => (
              <Switch
                checked={Boolean(effective[key])}
                disabled={change.isPending}
                aria-labelledby={labelId}
                aria-describedby={descriptionId}
                onCheckedChange={(next) => change.mutate({ [key]: next })}
              />
            )}
          />
        ))}
        <SettingSwitchRow
          label={t("number_grounding.label")}
          description={t("number_grounding.description")}
          sourceLabel={overridden("number_grounding") ? t("workspace") : t("default")}
          overridden={overridden("number_grounding")}
          useDefaultLabel={t("useDefault")}
          busy={change.isPending}
          onUseDefault={() => change.mutate({ number_grounding: null })}
          control={({ labelId }) => (
            <RadioGroup
              aria-labelledby={labelId}
              value={
                typeof effective.number_grounding === "string" ? effective.number_grounding : "off"
              }
              disabled={change.isPending}
              onValueChange={(value) => change.mutate({ number_grounding: value })}
              options={GROUNDING.map((value) => ({ value, label: t(`number_grounding.${value}`) }))}
            />
          )}
        />
      </ul>
      {change.isError ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {errors(codeOf(change.error))}
        </p>
      ) : null}
    </div>
  );
}

const EVERY_BASE = "every";

function TestQuestion({ bases }: { bases: KnowledgeBase[] }) {
  const t = useTranslations("app.knowledge.search");
  const retry = useTranslations("app.knowledge");
  const errors = useCodeText();
  const counter = useCounter();
  const router = useRouter();
  const { session } = useSession();
  const [question, setQuestion] = useState("");
  const [base, setBase] = useState(EVERY_BASE);
  const questionId = useId();
  const search = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/knowledge/search", {
          body: {
            query: question.trim(),
            ...(base !== EVERY_BASE ? { knowledge_base_id: base } : {}),
          },
        }),
      ),
  });
  const code = codeOf(search.error);
  const matchLabel = (match: string | null | undefined) =>
    match === "strong" ? t("strong") : match === "partial" ? t("possible") : t("weak");
  const tooLong = question.length > QUESTION_MAX;
  return (
    <div className="flex flex-col gap-4" data-test-question="">
      <form
        noValidate
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (!question.trim() || tooLong || search.isPending) return;
          search.mutate();
        }}
      >
        <label htmlFor={questionId} className="text-sm font-medium text-text">
          {t("question")}
        </label>
        <Textarea
          id={questionId}
          dir="auto"
          rows={3}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          data-field="test-question"
        />
        <TextCounter
          length={question.length}
          max={QUESTION_MAX}
          text={counter(question.length, QUESTION_MAX)}
        />
        <div className="flex max-w-80 flex-col gap-1">
          <span id={`${questionId}-base`} className="text-sm font-medium text-text">
            {t("base")}
          </span>
          <Select
            aria-labelledby={`${questionId}-base`}
            value={base}
            onValueChange={setBase}
            options={[
              { value: EVERY_BASE, label: t("everyBase") },
              ...bases.map((row) => ({ value: row.id, label: row.name })),
            ]}
          />
        </div>
        <div>
          <Button
            type="submit"
            variant="secondary"
            busy={search.isPending}
            disabled={!question.trim() || tooLong}
          >
            {t("submit")}
          </Button>
        </div>
      </form>
      {search.isError ? (
        <div className="flex flex-wrap items-center gap-3">
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {code === "knowledge_search_unavailable" ? t("unavailable") : errors(code)}
          </p>
          <Button variant="ghost" onClick={() => search.mutate()}>
            {retry("retry")}
          </Button>
        </div>
      ) : null}
      {search.isSuccess ? (
        <div className="flex flex-col gap-3" aria-live="polite" data-search-result="">
          {search.data.is_empty ? (
            <p className="m-0 text-md text-text">{t("none")}</p>
          ) : (
            <>
              <p className="m-0 text-md font-semibold text-text">
                {t("found", { count: search.data.passages.length })} ·{" "}
                {matchLabel(search.data.match)}
              </p>
              <PassageList
                label={t("passages")}
                passages={search.data.passages.map((passage, index) => ({
                  key: `${passage.document_id ?? "x"}-${index}`,
                  title: passage.document_title,
                  text: passage.content,
                  match: "",
                  ...(passage.document_id && base !== EVERY_BASE
                    ? {
                        onOpen: () =>
                          router.push(knowledgeBaseHref(base, { doc: passage.document_id! })),
                      }
                    : {}),
                }))}
              />
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

function UnansweredQuestions() {
  const t = useTranslations("app.knowledge.gaps");
  const errors = useCodeText();
  const number = useNumber();
  const times = useTimes();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [days, setDays] = useState<(typeof GAP_DAYS)[number]>(30);
  const gaps = useQuery({
    queryKey: gapsKey(scope, days),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/knowledge/gaps", { params: { query: { days } } })),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });
  const off =
    gaps.isError &&
    gaps.error instanceof ApiError &&
    (gaps.error.code === "knowledge_gap_report_disabled" || gaps.error.status === 404);
  return (
    <div className="flex flex-col gap-4" data-gaps="">
      {off ? (
        <p className="m-0 text-md text-text" data-gaps-off="">
          {t("off")}
        </p>
      ) : (
        <>
          <SegmentedControl
            label={t("days")}
            value={String(days)}
            onChange={(value: string) => setDays(Number(value) as (typeof GAP_DAYS)[number])}
            options={GAP_DAYS.map((value) => ({
              value: String(value),
              label: t("daysOption", { days: value }),
            }))}
          />
          {gaps.isPending ? (
            <Loading />
          ) : gaps.isError ? (
            <p className="m-0 text-sm text-text">{errors(codeOf(gaps.error))}</p>
          ) : gaps.data.gaps.length ? (
            <GapList
              label={t("label")}
              searchesLabel={t("searches")}
              lastAskedLabel={t("lastAsked")}
              gaps={gaps.data.gaps.map((gap, index) => {
                const when = times(gap.last_asked_at);
                return {
                  key: `${index}-${gap.last_asked_at}`,
                  query: gap.query,
                  searches: number(gap.searches),
                  lastAsked: when?.time ?? "",
                  lastAskedFull: when?.full ?? "",
                };
              })}
            />
          ) : (
            <p className="m-0 text-md text-text">{t("none")}</p>
          )}
        </>
      )}
    </div>
  );
}

export function KnowledgeHome() {
  const admin = useIsAdmin();
  return admin ? <KnowledgeScreen /> : <NotForRole />;
}

function KnowledgeScreen() {
  const t = useTranslations("app.knowledge");
  const errors = useCodeText();
  const format = useFormatter();
  const bases = useBasesList();
  const [creating, setCreating] = useState(false);
  const rows = useMemo(() => bases.data?.pages.flatMap((page) => page.items) ?? [], [bases.data]);

  let body: ReactNode;
  if (bases.isError) {
    body = (
      <ErrorState
        headingLevel={2}
        title={errors(codeOf(bases.error))}
        requestId={bases.error instanceof ApiError ? bases.error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void bases.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (bases.isPending) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else if (!rows.length) {
    body = <EmptyState headingLevel={2} title={t("empty")} />;
  } else {
    body = (
      <Card as="div" data-bases-list="">
        <RecordList
          label={t("listLabel")}
          busy={bases.isFetchingNextPage}
          footer={
            bases.hasNextPage ? (
              <Button
                variant="secondary"
                busy={bases.isFetchingNextPage}
                onClick={() => void bases.fetchNextPage()}
              >
                {t("loadMore")}
              </Button>
            ) : null
          }
        >
          {rows
            .map((base) =>
              rowLink({
                href: knowledgeBaseHref(base.id),
                className:
                  "flex min-h-target w-full flex-col gap-1 rounded-lg px-4 py-3 text-start text-text no-underline hover:bg-secondary-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                children: (
                  <>
                    <span className="truncate text-md font-semibold">
                      <bdi>{base.name}</bdi>
                    </span>
                    {base.description ? (
                      <span className="truncate text-sm text-text-muted">
                        <bdi dir="auto">{base.description}</bdi>
                      </span>
                    ) : null}
                    <span className="text-sm text-text-muted">
                      {t("created")}{" "}
                      {format.dateTime(new Date(base.created_at), { dateStyle: "medium" })}
                    </span>
                  </>
                ),
              }),
            )
            .map((link, index) => (
              <li key={rows[index]!.id} data-base-row="">
                {link}
              </li>
            ))}
        </RecordList>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-6" data-screen="knowledge">
      {/* The page's head on the secondary band, as home's (the 30 %, VQ6-02). */}
      <div className="rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader
          title={t("title")}
          description={t("line")}
          actions={
            <Button onClick={() => setCreating(true)} data-action="new-base">
              {t("newBase")}
            </Button>
          }
        />
      </div>
      {body}
      <Disclosure id="answer-settings" title={t("settings.title")} line={t("settings.line")}>
        {() => <AnswerSettings />}
      </Disclosure>
      <Disclosure id="test-question" title={t("search.title")} line={t("search.line")}>
        {() => <TestQuestion bases={rows} />}
      </Disclosure>
      <Disclosure id="unanswered" title={t("gaps.title")} line={t("gaps.line")}>
        {() => <UnansweredQuestions />}
      </Disclosure>
      <CreateBaseDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}
