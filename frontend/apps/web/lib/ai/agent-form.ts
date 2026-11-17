/**
 * What the agent page sends (AG-04): a PATCH carries only the fields the person changed. The bounds
 * come from `GET /agents/options` (FEB-37) - never retyped here. `escalation_sentiment`'s "off" is
 * sent as `null` - a value, not an absence: the API tells the two apart (`AgentUpdate.was_sent`).
 * A description cannot be removed through the API, only changed. Pure, so every rule is unit-tested.
 */
import { toLatinDigits } from "../customers/lead-form";

/** The escalation levels a person may choose, and "off". `positive` is not offered (recorded). */
export const ESCALATION_CHOICES = ["off", "neutral", "negative", "angry"] as const;
export type EscalationChoice = (typeof ESCALATION_CHOICES)[number];

export interface AgentOptions {
  models: string[] | null;
  default_model: string;
  max_output_tokens: { default: number; ceiling: number };
  bounds: {
    name: { max_length: number };
    description: { max_length: number };
    system_prompt: { max_length: number };
    temperature: { min: number; max: number };
    memory_message_limit: { min: number; max: number };
    memory_token_budget: { min: number; max: number };
  };
  default_knowledge_grant: boolean;
}

export interface AgentShape {
  name: string;
  description: string | null;
  system_prompt: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
  memory_message_limit: number;
  memory_token_budget: number;
  escalation_sentiment: string | null;
}

/** The form as typed: numbers are text until they are checked. */
export interface AgentFormValues {
  name: string;
  description: string;
  system_prompt: string;
  model: string;
  temperature: string;
  max_output_tokens: string;
  memory_message_limit: string;
  memory_token_budget: string;
  escalation: EscalationChoice;
}

export const AGENT_FIELDS = [
  "name",
  "description",
  "system_prompt",
  "model",
  "temperature",
  "max_output_tokens",
  "memory_message_limit",
  "memory_token_budget",
  "escalation",
] as const;
export type AgentField = (typeof AGENT_FIELDS)[number];

export function escalationOf(value: string | null): EscalationChoice {
  return (ESCALATION_CHOICES as readonly string[]).includes(value ?? "off")
    ? ((value ?? "off") as EscalationChoice)
    : "off";
}

export function formOf(agent: AgentShape): AgentFormValues {
  return {
    name: agent.name,
    description: agent.description ?? "",
    system_prompt: agent.system_prompt,
    model: agent.model,
    temperature: String(agent.temperature),
    max_output_tokens: String(agent.max_output_tokens),
    memory_message_limit: String(agent.memory_message_limit),
    memory_token_budget: String(agent.memory_token_budget),
    escalation: escalationOf(agent.escalation_sentiment),
  };
}

export type Problem =
  | "required"
  | "tooLong"
  | "notANumber"
  | "outOfRange"
  | "notAWholeNumber"
  | "descriptionCannotBeRemoved"
  | "modelNotOffered";

function number(text: string): number | null {
  const plain = toLatinDigits(text).trim();
  if (!/^\d+(\.\d+)?$/.test(plain)) return null;
  return Number(plain);
}

function inRange(
  text: string,
  bounds: { min: number; max: number },
  whole: boolean,
): Problem | null {
  const value = number(text);
  if (value === null) return "notANumber";
  if (whole && !Number.isInteger(value)) return "notAWholeNumber";
  if (value < bounds.min || value > bounds.max) return "outOfRange";
  return null;
}

/** What is wrong with each field, by the deployment's own bounds; nothing sent while any remains. */
export function agentProblems(
  values: AgentFormValues,
  original: AgentShape,
  options: AgentOptions,
): Partial<Record<AgentField, Problem>> {
  const problems: Partial<Record<AgentField, Problem>> = {};
  const { bounds } = options;
  const name = values.name.trim();
  if (!name) problems.name = "required";
  else if (name.length > bounds.name.max_length) problems.name = "tooLong";
  const description = values.description.trim();
  if (!description && original.description) problems.description = "descriptionCannotBeRemoved";
  else if (description.length > bounds.description.max_length) problems.description = "tooLong";
  if (!values.system_prompt.trim()) problems.system_prompt = "required";
  else if (values.system_prompt.length > bounds.system_prompt.max_length)
    problems.system_prompt = "tooLong";
  if (options.models !== null && values.model !== original.model) {
    if (!options.models.includes(values.model)) problems.model = "modelNotOffered";
  }
  const temperature = inRange(values.temperature, bounds.temperature, false);
  if (temperature) problems.temperature = temperature;
  const tokens = inRange(
    values.max_output_tokens,
    { min: 1, max: options.max_output_tokens.ceiling },
    true,
  );
  if (tokens) problems.max_output_tokens = tokens;
  const messages = inRange(values.memory_message_limit, bounds.memory_message_limit, true);
  if (messages) problems.memory_message_limit = messages;
  const budget = inRange(values.memory_token_budget, bounds.memory_token_budget, true);
  if (budget) problems.memory_token_budget = budget;
  return problems;
}

export type AgentPatch = Partial<{
  name: string;
  description: string;
  system_prompt: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
  memory_message_limit: number;
  memory_token_budget: number;
  escalation_sentiment: Exclude<EscalationChoice, "off"> | null;
}>;

/**
 * Only what changed. An untouched field is omitted; "off" is `null`; the model is sent only when
 * the deployment offers a choice. Nothing changed → an empty object (and no request).
 */
export function agentPatch(original: AgentShape, values: AgentFormValues): AgentPatch {
  const patch: AgentPatch = {};
  const name = values.name.trim();
  if (name !== original.name) patch.name = name;
  const description = values.description.trim();
  if (description && description !== (original.description ?? "")) patch.description = description;
  if (values.system_prompt !== original.system_prompt) patch.system_prompt = values.system_prompt;
  if (values.model !== original.model) patch.model = values.model;
  const temperature = number(values.temperature);
  if (temperature !== null && temperature !== original.temperature) patch.temperature = temperature;
  const tokens = number(values.max_output_tokens);
  if (tokens !== null && tokens !== original.max_output_tokens) patch.max_output_tokens = tokens;
  const messages = number(values.memory_message_limit);
  if (messages !== null && messages !== original.memory_message_limit)
    patch.memory_message_limit = messages;
  const budget = number(values.memory_token_budget);
  if (budget !== null && budget !== original.memory_token_budget)
    patch.memory_token_budget = budget;
  const escalation = values.escalation === "off" ? null : values.escalation;
  if (escalation !== original.escalation_sentiment) patch.escalation_sentiment = escalation;
  return patch;
}

export function isDirty(original: AgentShape, values: AgentFormValues): boolean {
  return Object.keys(agentPatch(original, values)).length > 0 || hasUnsavedText(original, values);
}

/** Text typed that is not (yet) a change the API would accept still counts as unsaved. */
function hasUnsavedText(original: AgentShape, values: AgentFormValues): boolean {
  const before = formOf(original);
  return AGENT_FIELDS.some((field) => values[field] !== before[field]);
}

/** The scope of `search_knowledge`: every base is `null`, never an empty list (TL-03). */
export function knowledgeScopeConfig(
  choice: "all" | "chosen",
  chosen: readonly string[],
): { knowledge_base_ids: string[] } | null {
  if (choice === "all") return null;
  return { knowledge_base_ids: [...new Set(chosen)] };
}
