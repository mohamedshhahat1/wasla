"use client";

/**
 * APP-CUS-03 (LD-01 .. LD-10): one lead. Each section saves on its own and sends only what the
 * person changed in it - an untouched field is omitted, an emptied one is null (docs/CRM.md).
 * The status moves only along `next_statuses`, with the status the page shows; an assignment
 * names the owner the page shows (null is a value). A change a colleague made first is told, not
 * overwritten. Nothing is optimistic; a control a role cannot use is absent.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Cog, User } from "lucide-react";
import NextLink from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import {
  Banner,
  Button,
  Card,
  EmptyState,
  ErrorState,
  FieldConfirmedMark,
  FormField,
  Input,
  KeyValueList,
  LinkConfirm,
  NoteComposer,
  NoteList,
  RecordHeader,
  ScoreField,
  Select,
  Textarea,
  Timeline,
  buttonClasses,
} from "@wasla/ui";

import {
  CONTACT_LEADS,
  LEADS,
  LEAD_STATISTICS,
  leadActivityKey,
  leadNotesKey,
  resourcePrefix,
} from "../../lib/customers/keys";
import {
  assignmentBody,
  formOf,
  hasNul,
  LEAD_BOUNDS,
  leadPatch,
  leadProblems,
  type LeadFormValues,
  type LeadPatch,
} from "../../lib/customers/lead-form";
import { contactHref } from "../../lib/customers/view";
import { leadKey } from "../../lib/inbox/keys";
import { openConfirmed, splitLinks } from "../../lib/inbox/text";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { useProblemText } from "./lead-create-dialog";
import { LeadStatusControl } from "./lead-status";
import {
  codeOf,
  isNotFound,
  PAGE_SIZE,
  useCounter,
  useIsAdmin,
  useNumber,
  usePeople,
  useTimes,
  type LeadActivity,
  type LeadRead,
} from "./shared";
import { TagEditor } from "./tag-editor";

const DETAILS = ["name", "phone", "email"] as const;
const DEAL = ["interest", "budget_amount", "budget_currency"] as const;
const TAGS = ["tags"] as const;
const NONE = "none";

/** One section of the lead's fields with its own Save: only that section's changes are sent. */
function FieldSection({
  lead,
  fields,
  title,
  onSaved,
  children,
}: {
  lead: LeadRead;
  fields: ReadonlyArray<keyof LeadFormValues>;
  title: string;
  onSaved: (lead: LeadRead) => void;
  children: (form: {
    values: LeadFormValues;
    set: (field: keyof LeadFormValues) => (value: string) => void;
    setTags: (tags: string[]) => void;
    problem: (field: keyof LeadFormValues, max?: number) => string | undefined;
    confirmed: (field: string) => ReactNode;
  }) => ReactNode;
}) {
  const t = useTranslations("app.customers");
  const errors = useCodeText();
  const problemText = useProblemText();
  const { session } = useSession();
  const headingId = useId();
  const initial = useMemo(() => formOf(lead), [lead]);
  const [values, setValues] = useState<LeadFormValues>(initial);
  const [shown, setShown] = useState(false);
  // What is stored replaces the form whenever the lead is read again.
  useEffect(() => setValues(initial), [initial]);

  const problems = leadProblems(values, fields);
  const patch = leadPatch(initial, values, fields);
  const save = useMutation({
    mutationFn: (body: LeadPatch) =>
      unwrap(
        session.api.PATCH("/api/v1/leads/{lead_id}", {
          params: { path: { lead_id: lead.id } },
          body,
        }),
      ),
    onSuccess: (fresh) => {
      setShown(false);
      onSaved(fresh);
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setShown(true);
    if (!patch || Object.keys(problems).length || save.isPending) return;
    save.mutate(patch);
  };

  return (
    <Card as="section" aria-labelledby={headingId} data-section={fields[0]}>
      <form className="flex flex-col gap-5" onSubmit={submit} noValidate>
        <h2 id={headingId} className="m-0 text-xl font-semibold text-text">
          {title}
        </h2>
        {children({
          values,
          set: (field) => (value) => setValues((current) => ({ ...current, [field]: value })),
          setTags: (tags) => setValues((current) => ({ ...current, tags })),
          problem: (field, max) => (shown ? problemText(problems[field], max) : undefined),
          confirmed: (field) =>
            lead.human_verified_fields.includes(field) ? (
              <FieldConfirmedMark label={t("fields.confirmed")} />
            ) : null,
        })}
        {save.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(codeOf(save.error))}
          </p>
        ) : null}
        {patch ? (
          <div className="flex flex-wrap gap-3">
            <Button type="submit" variant="primary" busy={save.isPending} data-save-section="">
              {t("save")}
            </Button>
            <Button
              variant="secondary"
              disabled={save.isPending}
              onClick={() => {
                setValues(initial);
                setShown(false);
                save.reset();
              }}
            >
              {t("cancel")}
            </Button>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

/** One activity as a sentence: who did what, each value isolated; one ICU message per kind. */
function useActivitySentence() {
  const t = useTranslations("app.customers");
  const { nameOf } = usePeople();
  const number = useNumber();
  const locale = useLocale();
  const statusWord = (value: unknown) =>
    typeof value === "string" ? t(`leadStatus.${value}` as "leadStatus.new") : "";
  const shown = (value: unknown): string =>
    value === null || value === undefined || value === ""
      ? t("activity.nothing")
      : Array.isArray(value)
        ? value.join(", ") || t("activity.nothing")
        : typeof value === "string"
          ? value
          : typeof value === "number" || typeof value === "boolean"
            ? String(value)
            : JSON.stringify(value);
  function sentence(entry: LeadActivity): ReactNode {
    const data = (entry.data ?? {}) as Record<string, unknown>;
    const actor = entry.actor_kind;
    const tags = {
      name: () => <bdi>{nameOf(entry.actor_id) ?? t("people.former")}</bdi>,
    };
    switch (entry.kind) {
      case "created":
        return t.rich("activity.created", { actor, ...tags });
      case "status_changed":
        return t.rich("activity.statusChanged", {
          actor,
          ...tags,
          from: () => <bdi>{statusWord(data.from)}</bdi>,
          to: () => <bdi>{statusWord(data.to)}</bdi>,
        });
      case "assigned":
        return t.rich("activity.assigned", {
          actor,
          ...tags,
          to: () => (
            <bdi>{nameOf(typeof data.to === "string" ? data.to : null) ?? t("people.former")}</bdi>
          ),
        });
      case "unassigned":
        return data.cause === "member_revoked"
          ? t("activity.ownerLeft")
          : t.rich("activity.unassigned", { actor, ...tags });
      case "score_changed":
        return t.rich("activity.scoreChanged", {
          actor,
          ...tags,
          from: () => <bdi>{number(Number(data.from ?? 0))}</bdi>,
          to: () => <bdi>{number(Number(data.to ?? 0))}</bdi>,
        });
      case "note_added":
        return t.rich("activity.noteAdded", { actor, ...tags });
      case "fields_updated": {
        const changes = Object.entries(data).filter(
          ([, change]) => change !== null && typeof change === "object" && "to" in change,
        ) as Array<[string, { from?: unknown; to?: unknown }]>;
        const labels = changes.map(([field]) =>
          t.has(`activity.field.${field}` as "activity.field.name")
            ? t(`activity.field.${field}` as "activity.field.name")
            : field,
        );
        const list = new Intl.ListFormat(locale, { type: "conjunction" }).format(labels);
        return (
          <>
            {t.rich("activity.fieldsUpdated", { actor, ...tags, fields: () => <bdi>{list}</bdi> })}
            <span className="mt-1 flex flex-col gap-1 text-sm text-text-muted">
              {changes.map(([field, change], index) => (
                <span key={field} data-change={field}>
                  {t.rich("activity.change", {
                    field: labels[index] ?? field,
                    from: () => <bdi dir="auto">{shown(change.from)}</bdi>,
                    to: () => <bdi dir="auto">{shown(change.to)}</bdi>,
                  })}
                </span>
              ))}
            </span>
          </>
        );
      }
      default:
        return t.rich("activity.unknown", { actor, ...tags });
    }
  }
  return sentence;
}

export function LeadDetail({ id }: { id: string }) {
  const t = useTranslations("app.customers");
  const inbox = useTranslations("app.inbox");
  const errors = useCodeText();
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const admin = useIsAdmin();
  const times = useTimes();
  const counter = useCounter();
  const number = useNumber();
  const locale = useLocale();
  const { members, nameOf } = usePeople();
  const sentence = useActivitySentence();
  const ownerId = useId();
  const [note, setNote] = useState("");
  const [link, setLink] = useState<string | null>(null);
  const [staleOwner, setStaleOwner] = useState<string | null>(null);

  const lead = useQuery({
    queryKey: leadKey(scope, id),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/leads/{lead_id}", { params: { path: { lead_id: id } } })),
    retry: (count, error) => !isNotFound(error) && count < 2,
    refetchOnWindowFocus: true,
  });
  const found = lead.isSuccess;
  const notes = useInfiniteQuery({
    queryKey: leadNotesKey(scope, id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/leads/{lead_id}/notes", {
          params: {
            path: { lead_id: id },
            query: { limit: PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: found,
    refetchOnWindowFocus: true,
  });
  const activity = useInfiniteQuery({
    queryKey: leadActivityKey(scope, id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        session.api.GET("/api/v1/leads/{lead_id}/activity", {
          params: {
            path: { lead_id: id },
            query: { limit: PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: found,
    refetchOnWindowFocus: true,
  });

  /** What is now stored replaces the cache; the lists and the timeline are read again. */
  const stored = (fresh: LeadRead) => {
    queryClient.setQueryData(leadKey(scope, id), fresh);
    void queryClient.invalidateQueries({ queryKey: leadActivityKey(scope, id) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEADS) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEAD_STATISTICS) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONTACT_LEADS) });
  };

  const score = useMutation({
    mutationFn: (value: number) =>
      unwrap(
        session.api.POST("/api/v1/leads/{lead_id}/score", {
          params: { path: { lead_id: id } },
          body: { score: value },
        }),
      ),
    onSuccess: stored,
  });
  const assign = useMutation({
    mutationFn: (to: string | null) =>
      unwrap(
        session.api.POST("/api/v1/leads/{lead_id}/assignment", {
          params: { path: { lead_id: id } },
          body: assignmentBody(lead.data!, to),
        }),
      ),
    onMutate: () => setStaleOwner(null),
    onSuccess: stored,
    onError: async (error) => {
      const code = codeOf(error);
      if (code !== "stale_assignment" && code !== "assignee_not_active_member") return;
      void members.query.refetch();
      const fresh = await lead.refetch();
      if (code === "stale_assignment" && fresh.data) {
        setStaleOwner(nameOf(fresh.data.assigned_to_id) ?? t("people.unassigned"));
      }
    },
  });
  const addNote = useMutation({
    mutationFn: (body: string) =>
      unwrap(
        session.api.POST("/api/v1/leads/{lead_id}/notes", {
          params: { path: { lead_id: id } },
          body: { body },
        }),
      ),
    onSuccess: () => {
      setNote("");
      void queryClient.invalidateQueries({ queryKey: leadNotesKey(scope, id) });
      void queryClient.invalidateQueries({ queryKey: leadActivityKey(scope, id) });
    },
  });

  if (lead.isError) {
    const error = lead.error;
    if (isNotFound(error)) {
      return (
        <EmptyState
          headingLevel={1}
          title={t("lead.notFoundTitle")}
          description={t("lead.notFoundBody")}
          action={
            <NextLink href="/leads" className={buttonClasses("secondary")}>
              {t("lead.back")}
            </NextLink>
          }
        />
      );
    }
    return (
      <ErrorState
        headingLevel={1}
        title={t("lead.errorTitle")}
        description={errors(codeOf(error))}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void lead.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  }
  if (lead.isPending) {
    return (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("lead.loading")}
      </p>
    );
  }

  const data = lead.data;
  const owner = nameOf(data.assigned_to_id) ?? t("people.unassigned");
  const collator = new Intl.Collator(locale);
  const ownerOptions = [
    { value: NONE, label: t("people.unassigned") },
    ...[...members.byId.entries()]
      .map(([memberId, member]) => ({ value: memberId, label: member.name }))
      .sort((a, b) => collator.compare(a.label, b.label)),
  ];
  const noteRows = notes.data?.pages.flatMap((page) => page.items) ?? [];
  const activityRows = activity.data?.pages.flatMap((page) => page.items) ?? [];
  const custom = Object.entries((data.custom_fields ?? {}) as Record<string, unknown>);
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
  const noteSendable = note.trim().length > 0 && !hasNul(note);

  return (
    <div className="flex flex-col gap-6" data-screen="lead" data-lead-id={data.id}>
      <NextLink href="/leads" className="self-start text-sm font-medium text-text underline">
        {t("lead.back")}
      </NextLink>
      <RecordHeader
        title={data.name || t("leads.unnamed")}
        beside={<LeadStatusControl lead={data} />}
        sub={
          <span className="flex flex-wrap gap-x-4 gap-y-1">
            <span>
              {t("lead.source", { source: t(`leadSource.${data.source}` as "leadSource.manual") })}
            </span>
            <span data-lead-score="">{t("lead.score", { score: number(data.score) })}</span>
            <span data-lead-owner="">
              {t.rich("lead.owner", { name: () => <bdi>{owner}</bdi> })}
            </span>
          </span>
        }
        actions={
          <>
            {data.contact_id ? (
              <NextLink href={contactHref(data.contact_id)} className={buttonClasses("secondary")}>
                {t("lead.openCustomer")}
              </NextLink>
            ) : null}
            {data.conversation_id ? (
              <NextLink
                href={`/inbox/${data.conversation_id}`}
                className={buttonClasses("secondary")}
              >
                {t("lead.openConversation")}
              </NextLink>
            ) : null}
          </>
        }
      />
      {staleOwner ? (
        <Banner
          tone="info"
          name="stale-assignment"
          title={t.rich("lead.staleOwner", { name: () => <bdi>{staleOwner}</bdi> })}
        />
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="flex flex-col gap-6">
          <p className="m-0 text-sm text-text-muted">{t("fields.confirmedHint")}</p>
          <FieldSection lead={data} fields={DETAILS} title={t("lead.details")} onSaved={stored}>
            {({ values, set, problem, confirmed }) => (
              <>
                <FormField
                  label={t("fields.name")}
                  hint={confirmed("name")}
                  error={problem("name", LEAD_BOUNDS.name)}
                >
                  {(control) => (
                    <Input
                      {...control}
                      dir="auto"
                      autoComplete="off"
                      maxLength={LEAD_BOUNDS.name}
                      value={values.name}
                      onChange={(event) => set("name")(event.target.value)}
                      data-field="name"
                    />
                  )}
                </FormField>
                <FormField
                  label={t("fields.phone")}
                  hint={confirmed("phone")}
                  error={problem("phone")}
                >
                  {(control) => (
                    <Input
                      {...control}
                      dir="ltr"
                      inputMode="tel"
                      autoComplete="off"
                      maxLength={LEAD_BOUNDS.phone}
                      value={values.phone}
                      onChange={(event) => set("phone")(event.target.value)}
                      data-field="phone"
                    />
                  )}
                </FormField>
                <FormField
                  label={t("fields.email")}
                  hint={confirmed("email")}
                  error={problem("email")}
                >
                  {(control) => (
                    <Input
                      {...control}
                      dir="ltr"
                      inputMode="email"
                      autoComplete="off"
                      maxLength={LEAD_BOUNDS.email}
                      value={values.email}
                      onChange={(event) => set("email")(event.target.value)}
                      data-field="email"
                    />
                  )}
                </FormField>
              </>
            )}
          </FieldSection>

          <FieldSection lead={data} fields={DEAL} title={t("lead.deal")} onSaved={stored}>
            {({ values, set, problem, confirmed }) => (
              <>
                <FormField
                  label={t("fields.interest")}
                  hint={confirmed("interest")}
                  error={problem("interest", LEAD_BOUNDS.interest)}
                >
                  {(control) => (
                    <Textarea
                      {...control}
                      dir="auto"
                      rows={2}
                      maxLength={LEAD_BOUNDS.interest}
                      value={values.interest}
                      onChange={(event) => set("interest")(event.target.value)}
                      data-field="interest"
                    />
                  )}
                </FormField>
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                  <FormField
                    label={t("fields.budget")}
                    hint={confirmed("budget_amount") ?? t("fields.budgetHint")}
                    error={problem("budget_amount")}
                  >
                    {(control) => (
                      <Input
                        {...control}
                        dir="ltr"
                        inputMode="decimal"
                        autoComplete="off"
                        value={values.budget_amount}
                        onChange={(event) => set("budget_amount")(event.target.value)}
                        data-field="budget_amount"
                      />
                    )}
                  </FormField>
                  <FormField
                    label={t("fields.currency")}
                    hint={confirmed("budget_currency") ?? t("fields.currencyHint")}
                    error={problem("budget_currency")}
                  >
                    {(control) => (
                      <Input
                        {...control}
                        dir="ltr"
                        autoComplete="off"
                        maxLength={LEAD_BOUNDS.currency}
                        value={values.budget_currency}
                        onChange={(event) => set("budget_currency")(event.target.value)}
                        data-field="budget_currency"
                      />
                    )}
                  </FormField>
                </div>
              </>
            )}
          </FieldSection>

          <FieldSection lead={data} fields={TAGS} title={t("fields.tags")} onSaved={stored}>
            {({ values, setTags }) => <TagEditor tags={values.tags} onChange={setTags} />}
          </FieldSection>

          <Card as="section" data-section="score">
            <ScoreField
              label={t("lead.scoreLabel")}
              hint={t("lead.scoreHint", {
                min: number(LEAD_BOUNDS.scoreMin),
                max: number(LEAD_BOUNDS.scoreMax),
              })}
              value={data.score}
              min={LEAD_BOUNDS.scoreMin}
              max={LEAD_BOUNDS.scoreMax}
              saveLabel={t("save")}
              busy={score.isPending}
              error={score.isError ? errors(codeOf(score.error)) : undefined}
              onSave={(value) => score.mutate(value)}
            />
          </Card>

          <Card as="section" data-section="owner">
            <div className="flex flex-col gap-2">
              <span id={ownerId} className="text-sm font-medium text-text">
                {t("lead.ownerLabel")}
              </span>
              {admin ? (
                <div className="max-w-80">
                  <Select
                    aria-labelledby={ownerId}
                    value={data.assigned_to_id ?? NONE}
                    options={ownerOptions}
                    disabled={assign.isPending}
                    onValueChange={(value) => {
                      const to = value === NONE ? null : value;
                      if (to !== (data.assigned_to_id ?? null)) assign.mutate(to);
                    }}
                    data-owner-select=""
                  />
                </div>
              ) : (
                <span className="text-md text-text" data-owner-text="">
                  <bdi>{owner}</bdi>
                </span>
              )}
              {assign.isError && codeOf(assign.error) !== "stale_assignment" ? (
                <p role="alert" className="m-0 text-sm font-medium text-danger">
                  {errors(codeOf(assign.error))}
                </p>
              ) : null}
            </div>
          </Card>

          {custom.length ? (
            <Card as="section" data-section="custom">
              <h2 className="m-0 mb-4 text-xl font-semibold text-text">{t("lead.custom")}</h2>
              <KeyValueList
                items={custom.map(([key, value]) => ({
                  key,
                  label: <bdi dir="auto">{key}</bdi>,
                  value: (
                    <bdi dir="auto">
                      {typeof value === "string" ? value : JSON.stringify(value)}
                    </bdi>
                  ),
                }))}
              />
            </Card>
          ) : null}
        </div>

        <div className="flex flex-col gap-6">
          <Card as="section" data-section="notes">
            <h2 className="m-0 mb-4 text-xl font-semibold text-text">{t("notes.title")}</h2>
            <div className="flex flex-col gap-5">
              <NoteComposer
                label={t("notes.label")}
                hint={t("notes.hint")}
                value={note}
                maxLength={LEAD_BOUNDS.note}
                counter={counter}
                submitLabel={t("notes.add")}
                busy={addNote.isPending}
                error={addNote.isError ? errors(codeOf(addNote.error)) : undefined}
                onChange={setNote}
                onSubmit={() => {
                  if (noteSendable && !addNote.isPending) addNote.mutate(note.trim());
                }}
              />
              <NoteList
                label={t("notes.title")}
                empty={notes.isPending ? t("loading") : t("notes.empty")}
                notes={noteRows.map((item) => {
                  const at = times(item.created_at);
                  return {
                    key: item.id,
                    author:
                      item.author_kind === "agent"
                        ? t("people.ai")
                        : item.author_kind === "system"
                          ? t("people.system")
                          : (nameOf(item.author_id) ?? t("people.former")),
                    time: at?.time ?? "",
                    timeFull: at?.full ?? "",
                    parts: splitLinks(item.body),
                  };
                })}
                footer={more(notes)}
                onLink={setLink}
              />
            </div>
          </Card>

          <Card as="section" data-section="activity">
            <h2 className="m-0 mb-4 text-xl font-semibold text-text">{t("activity.title")}</h2>
            <Timeline
              label={t("activity.title")}
              empty={activity.isPending ? t("loading") : t("activity.empty")}
              entries={activityRows.map((entry) => {
                const at = times(entry.created_at);
                const icon =
                  entry.actor_kind === "agent" ? Bot : entry.actor_kind === "system" ? Cog : User;
                return {
                  key: entry.id,
                  icon,
                  iconName:
                    entry.actor_kind === "agent"
                      ? "Bot"
                      : entry.actor_kind === "system"
                        ? "Cog"
                        : "User",
                  what: sentence(entry),
                  time: at?.time ?? "",
                  timeFull: at?.full ?? "",
                };
              })}
              footer={more(activity)}
            />
          </Card>
        </div>
      </div>

      <LinkConfirm
        url={link}
        title={inbox("link.title")}
        description={inbox("link.description")}
        openLabel={inbox("link.open")}
        cancelLabel={inbox("link.cancel")}
        closeLabel={t("close")}
        onOpen={(url) => openConfirmed(url)}
        onClose={() => setLink(null)}
      />
    </div>
  );
}
