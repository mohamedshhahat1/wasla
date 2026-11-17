"use client";

/**
 * APP-AI-01, the agent's tools (TL-01 .. TL-04): one row per tool this build runs, its name from the
 * catalogue (a tool the catalogue does not know shows its API name and the API's description), a
 * switch for owners and administrators - PUT to grant, DELETE to withdraw - and the answer decides:
 * nothing is optimistic. search_knowledge's scope is every base (`null`) or the ones chosen. A grant
 * whose tool this build no longer runs is listed apart, with "Remove".
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { Button, KnowledgeScopePicker, ToolSwitchRow } from "@wasla/ui";

import { knowledgeScopeConfig } from "../../lib/ai/agent-form";
import { agentsKey, agentToolsKey, availableToolsKey } from "../../lib/ai/keys";
import { withHeaderCursor } from "../../lib/knowledge/bases";
import { basesKey } from "../../lib/knowledge/keys";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";

type ToolGrant = components["schemas"]["ToolGrantRead"];
type ToolSpec = components["schemas"]["ToolSpecRead"];
type KnowledgeBase = components["schemas"]["KnowledgeBaseRead"];

export const SEARCH_KNOWLEDGE = "search_knowledge";
const KNOWN_TOOLS = new Set([
  "request_human_handoff",
  SEARCH_KNOWLEDGE,
  "record_lead_details",
  "schedule_follow_up",
]);
const BASES_PAGE = 100;

function scopeOf(grant: ToolGrant | undefined): { choice: "all" | "chosen"; chosen: string[] } {
  const ids = (grant?.config as { knowledge_base_ids?: unknown } | null)?.knowledge_base_ids;
  return Array.isArray(ids) && ids.length
    ? { choice: "chosen", chosen: ids.map(String) }
    : { choice: "all", chosen: [] };
}

function useBases(enabled: boolean) {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useInfiniteQuery({
    queryKey: basesKey(scope),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      withHeaderCursor<KnowledgeBase>(
        session.api.GET("/api/v1/knowledge/bases", {
          params: { query: { limit: BASES_PAGE, ...(pageParam ? { cursor: pageParam } : {}) } },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.nextCursor,
    enabled,
  });
}

function ScopeEditor({
  agentId,
  grant,
  admin,
}: {
  agentId: string;
  grant: ToolGrant;
  admin: boolean;
}) {
  const t = useTranslations("app.ai.tools");
  const errors = useCodeText();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const bases = useBases(true);
  const saved = scopeOf(grant);
  const [choice, setChoice] = useState(saved.choice);
  const [chosen, setChosen] = useState(saved.chosen);
  useEffect(() => {
    setChoice(saved.choice);
    setChosen(saved.chosen);
    // The saved scope changes only when the grant is read again.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `saved` is derived from `grant`
  }, [grant]);
  const rows = useMemo(() => bases.data?.pages.flatMap((page) => page.items) ?? [], [bases.data]);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.PUT("/api/v1/agents/{agent_id}/tools", {
          params: { path: { agent_id: agentId } },
          body: {
            name: SEARCH_KNOWLEDGE,
            enabled: true,
            config: knowledgeScopeConfig(choice, chosen),
          },
        }),
      ),
    onSettled: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: agentToolsKey(scope, agentId) }),
        client.invalidateQueries({ queryKey: basesKey(scope) }),
        client.invalidateQueries({ queryKey: agentsKey(scope) }),
      ]),
  });
  const code = save.error instanceof ApiError ? save.error.code : null;

  if (!admin) {
    const names = rows.filter((base) => saved.chosen.includes(base.id)).map((base) => base.name);
    return (
      <p className="m-0 text-sm text-text" data-scope-read="">
        {saved.choice === "all"
          ? t("scope.readAll")
          : t.rich("scope.readChosen", { names: () => <bdi>{names.join(", ")}</bdi> })}
      </p>
    );
  }
  const noneChosen = choice === "chosen" && chosen.length === 0;
  const unchanged =
    choice === saved.choice &&
    (choice === "all" ||
      (chosen.length === saved.chosen.length && chosen.every((id) => saved.chosen.includes(id))));
  return (
    <KnowledgeScopePicker
      legend={t("scope.legend")}
      allLabel={t("scope.all")}
      chosenLabel={t("scope.chosen")}
      choice={choice}
      bases={rows.map((base) => ({ id: base.id, name: base.name }))}
      chosen={chosen}
      disabled={save.isPending}
      problem={
        noneChosen
          ? t("scope.none")
          : code === "knowledge_scope_invalid"
            ? errors(code)
            : save.isError
              ? errors(code)
              : undefined
      }
      onChoiceChange={setChoice}
      onChosenChange={setChosen}
      footer={
        <div className="flex flex-wrap items-center gap-3">
          {choice === "chosen" && !rows.length && bases.isSuccess ? (
            <p className="m-0 text-sm text-text-muted">{t("scope.noBases")}</p>
          ) : null}
          {bases.hasNextPage ? (
            <Button
              variant="ghost"
              busy={bases.isFetchingNextPage}
              onClick={() => void bases.fetchNextPage()}
            >
              {t("scope.more")}
            </Button>
          ) : null}
          <Button
            variant="secondary"
            busy={save.isPending}
            disabled={noneChosen || unchanged}
            onClick={() => save.mutate()}
            data-action="save-scope"
          >
            {t("scope.save")}
          </Button>
        </div>
      }
    />
  );
}

export function AgentTools({ agentId, admin }: { agentId: string; admin: boolean }) {
  const t = useTranslations("app.ai");
  const errors = useCodeText();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const available = useQuery({
    queryKey: availableToolsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/agents/available-tools")),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const grants = useQuery({
    queryKey: agentToolsKey(scope, agentId),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/agents/{agent_id}/tools", {
          params: { path: { agent_id: agentId } },
        }),
      ),
  });
  const [pending, setPending] = useState<string | null>(null);
  const [failed, setFailed] = useState<{ name: string; code: string | null } | null>(null);

  const toggle = useMutation({
    mutationFn: async ({ name, on }: { name: string; on: boolean }) => {
      if (on) {
        return unwrap(
          session.api.PUT("/api/v1/agents/{agent_id}/tools", {
            params: { path: { agent_id: agentId } },
            body: { name, enabled: true },
          }),
        );
      }
      try {
        return await unwrap(
          session.api.DELETE("/api/v1/agents/{agent_id}/tools/{name}", {
            params: { path: { agent_id: agentId, name } },
          }),
        );
      } catch (error) {
        // Already off (another tab): the list is read again and nothing is said.
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    onMutate: ({ name }) => {
      setPending(name);
      setFailed(null);
    },
    onError: (error, { name }) =>
      setFailed({ name, code: error instanceof ApiError ? error.code : null }),
    onSettled: async () => {
      setPending(null);
      await Promise.all([
        client.invalidateQueries({ queryKey: agentToolsKey(scope, agentId) }),
        client.invalidateQueries({ queryKey: agentsKey(scope) }),
      ]);
    },
  });

  if (available.isPending || grants.isPending) {
    return (
      <p role="status" className="m-0 text-sm text-text-muted">
        {t("loading")}
      </p>
    );
  }
  if (available.isError || grants.isError) {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="m-0 text-sm text-text">{t("loadError")}</p>
        <Button
          variant="secondary"
          onClick={() => {
            void available.refetch();
            void grants.refetch();
          }}
        >
          {t("retry")}
        </Button>
      </div>
    );
  }

  const byName = new Map(grants.data.map((grant) => [grant.name, grant]));
  const runs = new Set(available.data.map((spec) => spec.name));
  const gone = grants.data.filter((grant) => grant.enabled && !runs.has(grant.name));
  const label = (spec: ToolSpec) =>
    KNOWN_TOOLS.has(spec.name)
      ? {
          label: t(`tools.${spec.name}.label` as "tools.search_knowledge.label"),
          description: t(`tools.${spec.name}.description` as "tools.search_knowledge.description"),
          apiName: null,
        }
      : {
          label: spec.name,
          description: spec.description || t("tools.unknown"),
          apiName: spec.name,
        };

  return (
    <div className="flex flex-col gap-4" data-agent-tools="">
      <p className="m-0 text-sm text-text-muted">{t("tools.line")}</p>
      <ul aria-label={t("tools.label")} className="m-0 flex list-none flex-col gap-2 p-0">
        {available.data.map((spec) => {
          const grant = byName.get(spec.name);
          const on = Boolean(grant?.enabled);
          const words = label(spec);
          return (
            <ToolSwitchRow
              key={spec.name}
              label={words.label}
              apiName={words.apiName}
              description={words.description}
              on={on}
              readOnly={!admin}
              stateLabel={on ? t("on") : t("off")}
              busy={pending === spec.name}
              error={failed?.name === spec.name ? errors(failed.code) : undefined}
              onChange={(next) => toggle.mutate({ name: spec.name, on: next })}
            >
              {spec.name === SEARCH_KNOWLEDGE && on && grant ? (
                <ScopeEditor agentId={agentId} grant={grant} admin={admin} />
              ) : null}
            </ToolSwitchRow>
          );
        })}
      </ul>
      {gone.length ? (
        <div className="flex flex-col gap-2" data-tools-gone="">
          <h3 className="m-0 text-md font-semibold text-text">{t("tools.gone")}</h3>
          <p className="m-0 text-sm text-text-muted">{t("tools.goneLine")}</p>
          <ul aria-label={t("tools.gone")} className="m-0 flex list-none flex-col gap-2 p-0">
            {gone.map((grant) => (
              <ToolSwitchRow
                key={grant.name}
                label={grant.name}
                apiName={grant.name}
                description={t("tools.gone")}
                on
                readOnly
                stateLabel={t("on")}
                action={
                  admin ? (
                    <Button
                      variant="secondary"
                      busy={pending === grant.name}
                      onClick={() => toggle.mutate({ name: grant.name, on: false })}
                    >
                      {t("tools.remove")}
                    </Button>
                  ) : undefined
                }
              />
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
