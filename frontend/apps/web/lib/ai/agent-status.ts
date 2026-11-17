/**
 * An agent's status and the default that answers customers (AG-05, AG-06, HM6-01). The API's rules,
 * written once: only an ACTIVE agent marked default answers (`get_answering_default`); "Make
 * default" is offered only for an active agent (the API refuses the rest with `agent_not_active`);
 * disabling the answering agent stops every automatic answer, so it asks first. Pure.
 */

export const AGENT_STATUSES = ["draft", "active", "disabled"] as const;
export type AgentStatus = (typeof AGENT_STATUSES)[number];

export interface AgentLike {
  id: string;
  status: string;
  is_default: boolean;
}

export type AgentAction = "activate" | "makeDefault" | "disable";

/** Whether this agent answers customers: the default, and active. */
export function answers(agent: Pick<AgentLike, "status" | "is_default">): boolean {
  return agent.is_default && agent.status === "active";
}

/** The agent that answers customers, if any. */
export function answeringAgent<T extends AgentLike>(agents: readonly T[]): T | null {
  return agents.find(answers) ?? null;
}

/** The actions a person may take on an agent, in the order they are offered. */
export function actionsFor(agent: Pick<AgentLike, "status" | "is_default">): AgentAction[] {
  if (agent.status === "active") return agent.is_default ? ["disable"] : ["makeDefault", "disable"];
  return ["activate"];
}

/** Only turning off the agent that answers stops answers; it is the one action that asks first. */
export function needsDisableConfirmation(
  agent: Pick<AgentLike, "status" | "is_default">,
  action: AgentAction,
): boolean {
  return action === "disable" && answers(agent);
}

/** Which sentence describes the agent's state (the catalogue's `ai.status.*`). */
export type StatusSentence = "draft" | "activeIdle" | "answering" | "disabled" | "disabledDefault";

export function statusSentence(agent: Pick<AgentLike, "status" | "is_default">): StatusSentence {
  if (agent.status === "active") return agent.is_default ? "answering" : "activeIdle";
  if (agent.status === "disabled") return agent.is_default ? "disabledDefault" : "disabled";
  return "draft";
}

/** The PATCH an action sends; "makeDefault" is its own POST (null here). */
export function actionBody(action: AgentAction): { status: AgentStatus } | null {
  if (action === "activate") return { status: "active" };
  if (action === "disable") return { status: "disabled" };
  return null;
}

/**
 * Why no agent answers (the home warning, HM6-01): none answers at all, or the default exists but is
 * not active (it can be turned back on). Null when an agent answers.
 */
export type NoAnswer = { kind: "none" } | { kind: "defaultInactive"; agentId: string };

export function noAnsweringAgent(agents: readonly AgentLike[]): NoAnswer | null {
  if (answeringAgent(agents)) return null;
  const idleDefault = agents.find((agent) => agent.is_default);
  return idleDefault ? { kind: "defaultInactive", agentId: idleDefault.id } : { kind: "none" };
}
