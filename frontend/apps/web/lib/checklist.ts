/**
 * The onboarding checklist (APP-WS-03, HM-03), worked out from the workspace's own data - no
 * backend state, nothing stored. Five reads in, five states out; a read that failed makes its step
 * "unknown" (could not check), never "done".
 */
import type { SubscriptionStatus } from "./gates";

export type StepState = "done" | "undone" | "unknown";

export const CHECKLIST_STEPS = ["channel", "agent", "knowledge", "team", "plan"] as const;
export type ChecklistStep = (typeof CHECKLIST_STEPS)[number];

/** A read's outcome: the data, or "error" when it failed, or null while loading. */
export type Read<T> = T | "error" | null;

export interface ChecklistInputs {
  connections: Read<ReadonlyArray<{ status: string }>>;
  agents: Read<ReadonlyArray<{ is_default: boolean }>>;
  bases: Read<ReadonlyArray<unknown>>;
  members: Read<ReadonlyArray<{ status: string }>>;
  subscriptionStatus: Read<SubscriptionStatus | "none">;
}

function state<T>(read: Read<T>, done: (value: T) => boolean): StepState {
  if (read === "error" || read === null) return "unknown";
  return done(read) ? "done" : "undone";
}

export function checklistStates(inputs: ChecklistInputs): Record<ChecklistStep, StepState> {
  return {
    channel: state(inputs.connections, (rows) => rows.some((row) => row.status === "active")),
    // A new workspace answers nothing until a default agent exists (plan J2).
    agent: state(inputs.agents, (rows) => rows.some((row) => row.is_default)),
    // The list carries no document count: "at least one knowledge base" (recorded, HM-03).
    knowledge: state(inputs.bases, (rows) => rows.length > 0),
    team: state(inputs.members, (rows) => rows.filter((row) => row.status === "active").length > 1),
    plan: state(
      inputs.subscriptionStatus,
      (status) => status === "active" || status === "trialing",
    ),
  };
}

/** True while any step is not done (unknown counts as not done: the list stays). */
export function checklistOpen(states: Record<ChecklistStep, StepState>): boolean {
  return CHECKLIST_STEPS.some((step) => states[step] !== "done");
}

/** True while any of the five reads is still loading: the list shows no state until all answer. */
export function checklistLoading(inputs: ChecklistInputs): boolean {
  return [
    inputs.connections,
    inputs.agents,
    inputs.bases,
    inputs.members,
    inputs.subscriptionStatus,
  ].some((read) => read === null);
}
