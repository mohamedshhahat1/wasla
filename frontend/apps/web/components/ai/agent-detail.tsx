"use client";

/**
 * APP-AI-01, the agent page (AG-03 .. AG-06): who it is, what it is told, how it behaves, what it may
 * use and whether it answers - in that order. Owners and administrators edit Identity, Instructions
 * and Behaviour in one form whose PATCH carries only what changed (lib/ai/agent-form.ts); a member
 * reads the same sections as text and sees no control. The model is offered only from the
 * deployment's list (FEB-37); with no list it is text. Status moves and "Make default" are their own
 * requests; "Make default" only for an active agent; turning off the agent that answers asks first.
 * Nothing is optimistic: the page re-reads after every answer.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import {
  AnswersMark,
  cn,
  Banner,
  Button,
  Card,
  Dialog,
  ErrorState,
  FormField,
  Input,
  KeyValueList,
  RadioGroup,
  RecordHeader,
  RecordPanel,
  Select,
  TextCounter,
  Textarea,
} from "@wasla/ui";

import {
  agentPatch,
  agentProblems,
  ESCALATION_CHOICES,
  formOf,
  isDirty,
  type AgentField,
  type AgentFormValues,
} from "../../lib/ai/agent-form";
import {
  actionBody,
  actionsFor,
  answers,
  needsDisableConfirmation,
  statusSentence,
  type AgentAction,
} from "../../lib/ai/agent-status";
import { agentKey, agentsKey } from "../../lib/ai/keys";
import { useWorkspace } from "../app/workspace-context";
import { codeOf, isNotFound, useCounter, useIsAdmin, useNumber } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { AgentTools } from "./agent-tools";
import {
  AgentStatusBadge,
  useAgentOptions,
  type AgentOptionsRead,
  type AgentRead,
} from "./agents-list";

const ESCALATION_LABEL: Record<(typeof ESCALATION_CHOICES)[number], string> = {
  off: "fields.escalationOff",
  neutral: "fields.escalationNeutral",
  negative: "fields.escalationNegative",
  angry: "fields.escalationAngry",
};

/** The field a 422's `details` names, when it names one. */
function fieldOf(error: unknown): AgentField | null {
  if (!(error instanceof ApiError)) return null;
  const details = error.details as { errors?: Array<{ loc?: unknown[] }> } | null | undefined;
  const loc = details?.errors?.[0]?.loc;
  const name = Array.isArray(loc) ? String(loc[loc.length - 1]) : null;
  if (name === "system_prompt") return "system_prompt";
  if (name === "escalation_sentiment") return "escalation";
  return (
    (
      [
        "name",
        "description",
        "model",
        "temperature",
        "max_output_tokens",
        "memory_message_limit",
        "memory_token_budget",
      ] as const
    ).find((field) => field === name) ?? null
  );
}

function Section({ title, id, children }: { title: string; id: string; children: ReactNode }) {
  return (
    <Card as="section" aria-labelledby={id} data-agent-section={id}>
      <div className="flex flex-col gap-5">
        <h2 id={id} className="m-0 text-xl font-semibold text-text">
          {title}
        </h2>
        {children}
      </div>
    </Card>
  );
}

/** Leaving with unsaved changes asks first: the tab's own prompt, and a dialog inside the app. */
function useUnsavedGuard(dirty: boolean, onAsk: (href: string) => void) {
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey) return;
      const anchor = (event.target as Element | null)?.closest?.("a[href]");
      if (!(anchor instanceof HTMLAnchorElement) || anchor.target === "_blank") return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || url.pathname === window.location.pathname)
        return;
      event.preventDefault();
      event.stopPropagation();
      onAsk(url.pathname + url.search);
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", click, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", click, true);
    };
  }, [dirty, onAsk]);
}

function ReadOnlyAgent({ agent }: { agent: AgentRead }) {
  const t = useTranslations("app.ai");
  const number = useNumber();
  const escalation = agent.escalation_sentiment ?? "off";
  return (
    <>
      <p className="m-0 text-sm text-text-muted" data-read-only="">
        {t("readOnly")}
      </p>
      <Section title={t("sections.identity")} id="agent-identity">
        <KeyValueList
          items={[
            { key: "name", label: t("fields.name"), value: <bdi>{agent.name}</bdi> },
            ...(agent.description
              ? [
                  {
                    key: "description",
                    label: t("fields.description"),
                    value: <bdi dir="auto">{agent.description}</bdi>,
                  },
                ]
              : []),
          ]}
        />
      </Section>
      <Section title={t("sections.instructions")} id="agent-instructions">
        <p
          dir="auto"
          className="m-0 whitespace-pre-wrap text-md text-text"
          data-instructions-text=""
        >
          {agent.system_prompt}
        </p>
      </Section>
      <Section title={t("sections.behaviour")} id="agent-behaviour">
        <KeyValueList
          items={[
            { key: "model", label: t("fields.model"), value: <bdi dir="ltr">{agent.model}</bdi> },
            {
              key: "temperature",
              label: t("fields.temperature"),
              value: <bdi>{number(agent.temperature)}</bdi>,
            },
            {
              key: "tokens",
              label: t("fields.maxOutputTokens"),
              value: <bdi>{number(agent.max_output_tokens)}</bdi>,
            },
            {
              key: "messages",
              label: t("fields.memoryMessages"),
              value: <bdi>{number(agent.memory_message_limit)}</bdi>,
            },
            {
              key: "budget",
              label: t("fields.memoryBudget"),
              value: <bdi>{number(agent.memory_token_budget)}</bdi>,
            },
            {
              key: "escalation",
              label: t("fields.escalation"),
              value: t(ESCALATION_LABEL[escalation as "off"] ?? "fields.escalationOff"),
            },
          ]}
        />
      </Section>
    </>
  );
}

function AgentForm({ agent, options }: { agent: AgentRead; options: AgentOptionsRead }) {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const counter = useCounter();
  const number = useNumber();
  const router = useRouter();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [values, setValues] = useState<AgentFormValues>(() => formOf(agent));
  const [tried, setTried] = useState(false);
  const [leaving, setLeaving] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const base = useRef(agent);
  // A re-read after a save resets the form to what the API now holds.
  useEffect(() => {
    if (agent.updated_at !== base.current.updated_at) {
      base.current = agent;
      setValues(formOf(agent));
      setTried(false);
    }
  }, [agent]);

  const dirty = isDirty(agent, values);
  const problems = agentProblems(values, agent, options);
  const ask = useMemo(() => (href: string) => setLeaving(href), []);
  useUnsavedGuard(dirty, ask);
  const set = (field: AgentField) => (value: string) => {
    setSaved(false);
    setValues((current) => ({ ...current, [field]: value }));
  };

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.PATCH("/api/v1/agents/{agent_id}", {
          params: { path: { agent_id: agent.id } },
          body: agentPatch(agent, values),
        }),
      ),
    onSuccess: async (updated) => {
      client.setQueryData(agentKey(scope, agent.id), updated);
      setSaved(true);
      await client.invalidateQueries({ queryKey: agentsKey(scope) });
    },
  });
  const code = codeOf(save.error);
  const failedField = fieldOf(save.error);
  const fieldError = (field: AgentField): ReactNode => {
    if (field === "name" && code === "agent_name_exists") return errors(code);
    if (field === "model" && code === "agent_model_not_permitted") return errors(code);
    if (field === "max_output_tokens" && code === "agent_output_tokens_exceed_ceiling")
      return errors(code);
    if (failedField === field) return errors(code);
    const problem = problems[field];
    return (tried || values[field] !== formOf(agent)[field]) && problem
      ? t(`problems.${problem}`)
      : undefined;
  };
  const formError =
    save.isError &&
    !failedField &&
    ![
      "agent_name_exists",
      "agent_model_not_permitted",
      "agent_output_tokens_exceed_ceiling",
    ].includes(code ?? "")
      ? errors(code)
      : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (Object.keys(problems).length || save.isPending) return;
    if (!Object.keys(agentPatch(agent, values)).length) return;
    save.mutate();
  };
  const discard = () => {
    setValues(formOf(agent));
    setTried(false);
    save.reset();
  };

  const { bounds } = options;
  const modelOptions =
    options.models?.map((model) => ({
      value: model,
      label: model === options.default_model ? t("fields.modelDefault", { model }) : model,
    })) ?? null;

  return (
    <form noValidate onSubmit={submit} className="flex flex-col gap-6" data-agent-form="">
      <Section title={t("sections.identity")} id="agent-identity">
        <FormField label={t("fields.name")} required error={fieldError("name")}>
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              value={values.name}
              onChange={(event) => set("name")(event.target.value)}
              data-field="name"
            />
          )}
        </FormField>
        <FormField
          label={t("fields.description")}
          hint={t("fields.descriptionHint")}
          error={fieldError("description")}
        >
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              value={values.description}
              onChange={(event) => set("description")(event.target.value)}
              data-field="description"
            />
          )}
        </FormField>
      </Section>

      <Section title={t("sections.instructions")} id="agent-instructions">
        <FormField
          label={t("fields.instructions")}
          hint={t("fields.instructionsHint")}
          required
          error={fieldError("system_prompt")}
        >
          {(control) => (
            <Textarea
              {...control}
              dir="auto"
              rows={12}
              value={values.system_prompt}
              onChange={(event) => set("system_prompt")(event.target.value)}
              data-field="system_prompt"
            />
          )}
        </FormField>
        <TextCounter
          length={values.system_prompt.length}
          max={bounds.system_prompt.max_length}
          text={counter(values.system_prompt.length, bounds.system_prompt.max_length)}
        />
      </Section>

      <Section title={t("sections.behaviour")} id="agent-behaviour">
        {modelOptions ? (
          <FormField
            label={t("fields.model")}
            hint={t("fields.modelHint")}
            error={fieldError("model")}
          >
            {(control) => (
              <Select
                id={control.id}
                {...(control["aria-describedby"]
                  ? { "aria-describedby": control["aria-describedby"] }
                  : {})}
                value={values.model}
                options={
                  modelOptions.some((option) => option.value === values.model)
                    ? modelOptions
                    : [
                        { value: values.model, label: values.model, disabled: true },
                        ...modelOptions,
                      ]
                }
                onValueChange={set("model")}
              />
            )}
          </FormField>
        ) : (
          <div className="flex flex-col gap-1" data-model-fixed="">
            <span className="text-sm font-medium text-text">{t("fields.model")}</span>
            <bdi dir="ltr" className="text-md text-text">
              {agent.model}
            </bdi>
            <span className="text-sm text-text-muted">{t("fields.modelFixed")}</span>
          </div>
        )}
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
          <FormField
            label={t("fields.temperature")}
            hint={t("fields.temperatureHint", {
              min: number(bounds.temperature.min),
              max: number(bounds.temperature.max),
            })}
            error={fieldError("temperature")}
          >
            {(control) => (
              <Input
                {...control}
                inputMode="decimal"
                dir="ltr"
                value={values.temperature}
                onChange={(event) => set("temperature")(event.target.value)}
                data-field="temperature"
              />
            )}
          </FormField>
          <FormField
            label={t("fields.maxOutputTokens")}
            hint={t("fields.maxOutputTokensHint", {
              max: number(options.max_output_tokens.ceiling),
            })}
            error={fieldError("max_output_tokens")}
          >
            {(control) => (
              <Input
                {...control}
                inputMode="numeric"
                dir="ltr"
                value={values.max_output_tokens}
                onChange={(event) => set("max_output_tokens")(event.target.value)}
                data-field="max_output_tokens"
              />
            )}
          </FormField>
          <FormField
            label={t("fields.memoryMessages")}
            hint={t("fields.memoryMessagesHint", {
              min: number(bounds.memory_message_limit.min),
              max: number(bounds.memory_message_limit.max),
            })}
            error={fieldError("memory_message_limit")}
          >
            {(control) => (
              <Input
                {...control}
                inputMode="numeric"
                dir="ltr"
                value={values.memory_message_limit}
                onChange={(event) => set("memory_message_limit")(event.target.value)}
                data-field="memory_message_limit"
              />
            )}
          </FormField>
          <FormField
            label={t("fields.memoryBudget")}
            hint={t("fields.memoryBudgetHint", {
              min: number(bounds.memory_token_budget.min),
              max: number(bounds.memory_token_budget.max),
            })}
            error={fieldError("memory_token_budget")}
          >
            {(control) => (
              <Input
                {...control}
                inputMode="numeric"
                dir="ltr"
                value={values.memory_token_budget}
                onChange={(event) => set("memory_token_budget")(event.target.value)}
                data-field="memory_token_budget"
              />
            )}
          </FormField>
        </div>
        <fieldset className="m-0 flex flex-col gap-2 border-0 p-0">
          <legend id="agent-escalation" className="mb-1 p-0 text-sm font-medium text-text">
            {t("fields.escalation")}
          </legend>
          <p className="m-0 text-sm text-text-muted">{t("fields.escalationHint")}</p>
          <RadioGroup
            aria-labelledby="agent-escalation"
            value={values.escalation}
            onValueChange={set("escalation")}
            options={ESCALATION_CHOICES.map((choice) => ({
              value: choice,
              label: t(ESCALATION_LABEL[choice]),
            }))}
          />
        </fieldset>
      </Section>

      <div
        // Sticky only while there is something to save: at rest it never covers the form (audit P2).
        className={cn(
          "z-10 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface p-4 shadow-sm",
          dirty && "sticky bottom-0",
        )}
        data-agent-actions=""
      >
        <Button type="submit" busy={save.isPending} disabled={!dirty} data-action="save-agent">
          {t("save")}
        </Button>
        {dirty ? (
          <Button variant="secondary" onClick={discard} data-action="discard-agent">
            {t("discard")}
          </Button>
        ) : null}
        <p role="status" aria-live="polite" className="m-0 text-sm text-text-muted">
          {saved && !dirty ? t("saved") : ""}
        </p>
        {formError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {formError}
          </p>
        ) : null}
      </div>

      <Dialog
        open={leaving !== null}
        onOpenChange={(open) => !open && setLeaving(null)}
        title={t("unsavedTitle")}
        description={t("unsavedBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setLeaving(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const href = leaving;
                discard();
                setLeaving(null);
                if (href) setTimeout(() => router.push(href), 0);
              }}
            >
              {t("unsavedConfirm")}
            </Button>
          </>
        }
      />
    </form>
  );
}

function StatusSection({ agent }: { agent: AgentRead }) {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [confirming, setConfirming] = useState(false);
  const act = useMutation({
    mutationFn: (action: AgentAction) => {
      const body = actionBody(action);
      return body
        ? unwrap(
            session.api.PATCH("/api/v1/agents/{agent_id}", {
              params: { path: { agent_id: agent.id } },
              body,
            }),
          )
        : unwrap(
            session.api.POST("/api/v1/agents/{agent_id}/default", {
              params: { path: { agent_id: agent.id } },
            }),
          );
    },
    onSettled: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: agentKey(scope, agent.id) }),
        client.invalidateQueries({ queryKey: agentsKey(scope) }),
      ]),
  });
  const run = (action: AgentAction) => {
    if (needsDisableConfirmation(agent, action)) {
      setConfirming(true);
      return;
    }
    act.mutate(action);
  };
  const sentence = statusSentence(agent);
  return (
    <div className="flex flex-col gap-4" data-agent-status={agent.status}>
      {sentence === "disabledDefault" ? (
        <Banner
          tone="warning"
          title={t("status.sentence.disabledDefault")}
          name="agent-default-off"
        />
      ) : (
        <p className="m-0 text-md text-text" data-status-sentence={sentence}>
          {t(`status.sentence.${sentence}`)}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        {actionsFor(agent).map((action) => (
          <Button
            key={action}
            variant="secondary"
            busy={act.isPending && act.variables === action}
            disabled={act.isPending}
            onClick={() => run(action)}
            data-action={action}
          >
            {t(`actions.${action}`)}
          </Button>
        ))}
      </div>
      {act.isError ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {errors(codeOf(act.error))}
        </p>
      ) : null}
      <Dialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("disableTitle")}
        description={t("disableBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirming(false)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirming(false);
                act.mutate("disable");
              }}
              data-action="confirm-disable"
            >
              {t("disableConfirm")}
            </Button>
          </>
        }
      />
    </div>
  );
}

export function AgentDetail({ id }: { id: string }) {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const admin = useIsAdmin();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const agent = useQuery({
    queryKey: agentKey(scope, id),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/agents/{agent_id}", { params: { path: { agent_id: id } } })),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const options = useAgentOptions();

  if (agent.isError || options.isError) {
    const error = agent.error ?? options.error;
    if (isNotFound(error) || (error instanceof ApiError && error.status === 422)) {
      return (
        <ErrorState
          headingLevel={1}
          title={t("notFound")}
          action={
            <NextLink href="/ai/agents" className="text-primary underline underline-offset-4">
              {t("backToList")}
            </NextLink>
          }
        />
      );
    }
    return (
      <ErrorState
        headingLevel={1}
        title={t("loadError")}
        description={errors(codeOf(error))}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button
            variant="secondary"
            onClick={() => {
              void agent.refetch();
              void options.refetch();
            }}
          >
            {t("retry")}
          </Button>
        }
      />
    );
  }
  if (agent.isPending || options.isPending) {
    return (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  }
  const data = agent.data;
  return (
    <div className="flex flex-col gap-6" data-screen="agent">
      <NextLink
        href="/ai/agents"
        className="self-start text-sm text-primary underline underline-offset-4"
      >
        {t("backToList")}
      </NextLink>
      <RecordHeader
        title={data.name}
        titleId="agent-title"
        beside={
          <>
            <AgentStatusBadge status={data.status} />
            {answers(data) ? <AnswersMark label={t("answers")} /> : null}
          </>
        }
        sub={data.description ? <bdi dir="auto">{data.description}</bdi> : null}
      />
      {data.knowledge_unreachable && admin ? (
        <Banner tone="warning" title={t("unreachable")} name="knowledge-unreachable" />
      ) : null}
      {admin ? <AgentForm agent={data} options={options.data} /> : <ReadOnlyAgent agent={data} />}
      <Section title={t("sections.tools")} id="agent-tools">
        <AgentTools agentId={data.id} admin={admin} />
      </Section>
      <RecordPanel aria-labelledby="agent-status-title" data-section="status">
        <div className="flex flex-col gap-4">
          <h2 id="agent-status-title" className="m-0 text-xl font-semibold text-text">
            {t("sections.status")}
          </h2>
          {admin ? (
            <StatusSection agent={data} />
          ) : (
            <p className="m-0 text-md text-text" data-status-sentence={statusSentence(data)}>
              {t(`status.sentence.${statusSentence(data)}`)}
            </p>
          )}
        </div>
      </RecordPanel>
    </div>
  );
}
