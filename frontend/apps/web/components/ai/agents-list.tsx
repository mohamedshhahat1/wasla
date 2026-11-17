"use client";

/**
 * APP-AI-01, the list (AG-02): the workspace's agents as the API sends them (by name, at most 100 -
 * the API has no cursor), each with its status in words and "Answers customers" on the one active
 * default. "New agent" is an owner's or administrator's - absent for a member, not disabled. A new
 * agent is a draft, as the API makes it; nothing here is optimistic.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useId, useState, type FormEvent } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import {
  AgentRow,
  AnswersMark,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  FormField,
  Input,
  PageHeader,
  RecordList,
  TextCounter,
  Textarea,
} from "@wasla/ui";

import { answers } from "../../lib/ai/agent-status";
import { agentOptionsKey, agentsKey } from "../../lib/ai/keys";
import { agentHref } from "../../lib/knowledge/view";
import { useWorkspace } from "../app/workspace-context";
import { rowLink, useCounter, useIsAdmin, useTimes } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";

export type AgentRead = components["schemas"]["AgentRead"];
export type AgentOptionsRead = components["schemas"]["AgentOptionsRead"];

/** The API's largest page; it has no cursor, so this is the list (recorded under "not taken"). */
export const AGENTS_LIMIT = 100;

export function useAgents() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useQuery({
    queryKey: agentsKey(scope),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/agents", { params: { query: { limit: AGENTS_LIMIT } } })),
  });
}

/** What this deployment lets an agent be (FEB-37): once per workspace and session. */
export function useAgentOptions() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useQuery({
    queryKey: agentOptionsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/agents/options")),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function AgentStatusBadge({ status }: { status: string }) {
  const t = useTranslations("app.ai");
  return (
    <Badge tone={status === "active" ? "success" : "neutral"} icon={status === "active"}>
      {t(`status.${status}` as "status.draft")}
    </Badge>
  );
}

function CreateAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const counter = useCounter();
  const router = useRouter();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const options = useAgentOptions();
  const formId = useId();
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [tried, setTried] = useState(false);
  const bounds = options.data?.bounds;
  const nameMax = bounds?.name.max_length ?? 0;
  const promptMax = bounds?.system_prompt.max_length ?? 0;
  const nameProblem = !name.trim() ? "required" : name.trim().length > nameMax ? "tooLong" : null;
  const promptProblem = !instructions.trim()
    ? "required"
    : instructions.length > promptMax
      ? "tooLong"
      : null;

  const create = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/agents", {
          body: { name: name.trim(), system_prompt: instructions },
        }),
      ),
    onSuccess: async (agent) => {
      await client.invalidateQueries({ queryKey: agentsKey(scope) });
      onOpenChange(false);
      router.push(agentHref(agent.id));
    },
  });
  const code = create.error instanceof ApiError ? create.error.code : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!bounds || nameProblem || promptProblem || create.isPending) return;
    create.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setName("");
          setInstructions("");
          setTried(false);
          create.reset();
        }
        onOpenChange(next);
      }}
      title={t("create.title")}
      description={t("create.description")}
      closeLabel={t("close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("cancel")}
          </Button>
          <Button type="submit" form={formId} busy={create.isPending} disabled={!bounds}>
            {t("create.submit")}
          </Button>
        </>
      }
    >
      <form id={formId} noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <FormField
          label={t("create.name")}
          required
          error={
            code === "agent_name_exists"
              ? errors(code)
              : tried && nameProblem
                ? t(`problems.${nameProblem}`)
                : undefined
          }
        >
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              value={name}
              onChange={(event) => setName(event.target.value)}
              data-field="agent-name"
            />
          )}
        </FormField>
        <FormField
          label={t("create.instructions")}
          hint={t("create.instructionsHint")}
          required
          error={tried && promptProblem ? t(`problems.${promptProblem}`) : undefined}
        >
          {(control) => (
            <Textarea
              {...control}
              dir="auto"
              rows={6}
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              data-field="agent-instructions"
            />
          )}
        </FormField>
        {bounds ? (
          <TextCounter
            length={instructions.length}
            max={promptMax}
            text={counter(instructions.length, promptMax)}
          />
        ) : null}
        {options.data?.default_knowledge_grant ? (
          <p className="m-0 text-sm text-text-muted">{t("create.knowledgeGrant")}</p>
        ) : null}
        {create.isError && code !== "agent_name_exists" ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(code)}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

export function AgentsList() {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const admin = useIsAdmin();
  const times = useTimes();
  const agents = useAgents();
  const [creating, setCreating] = useState(false);

  // One primary per view: an empty list offers its own "Create the first agent" instead.
  const newAgent =
    admin && agents.data?.length ? (
      <Button onClick={() => setCreating(true)} data-action="new-agent">
        {t("newAgent")}
      </Button>
    ) : null;

  let body: React.ReactNode;
  if (agents.isError) {
    const error = agents.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("loadError")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void agents.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (agents.isPending) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else if (!agents.data.length) {
    body = (
      <EmptyState
        headingLevel={2}
        title={t("empty")}
        action={
          admin ? (
            <Button onClick={() => setCreating(true)} data-action="first-agent">
              {t("emptyAction")}
            </Button>
          ) : undefined
        }
      />
    );
  } else {
    body = (
      <Card as="div" data-agents-list="">
        <RecordList label={t("listLabel")}>
          {agents.data.map((agent) => {
            const updated = times(agent.updated_at);
            return (
              <AgentRow
                key={agent.id}
                href={agentHref(agent.id)}
                name={agent.name}
                status={<AgentStatusBadge status={agent.status} />}
                answers={
                  answers(agent) ? (
                    <AnswersMark label={t("answers")} />
                  ) : agent.is_default ? (
                    <Badge tone="warning">{t("status.defaultIdle")}</Badge>
                  ) : null
                }
                model={agent.model}
                modelLabel={t("model")}
                time={updated?.time ?? null}
                timeFull={updated?.full ?? null}
                renderLink={rowLink}
              />
            );
          })}
        </RecordList>
        {agents.data.length >= AGENTS_LIMIT ? (
          <p className="m-0 mt-3 text-sm text-text-muted">{t("limit100")}</p>
        ) : null}
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-6" data-screen="agents">
      {/* The page's head on the secondary band, as home's (the 30 %, VQ6-02). */}
      <div className="flex flex-col gap-3 rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader title={t("title")} description={t("line")} actions={newAgent} />
        {admin && agents.data?.length ? (
          <p className="m-0 text-sm text-text-muted">{t("draftNote")}</p>
        ) : null}
      </div>
      {body}
      {admin ? <CreateAgentDialog open={creating} onOpenChange={setCreating} /> : null}
    </div>
  );
}
