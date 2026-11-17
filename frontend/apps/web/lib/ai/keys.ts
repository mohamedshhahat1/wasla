/**
 * The AI screens' query resources (plan section 21: `[wsId, sessionGen, resource, params]`). The
 * agents list keeps FE-3's "agents" resource, so home, the checklist and the agents screen read and
 * invalidate one entry. The options and the tool catalogue are the deployment's: read once per
 * workspace and session.
 */
import { queryKey, type QueryKey, type QueryScope } from "@wasla/api-client";

export const AGENTS = "agents";
export const AGENT = "agent";
export const AGENT_TOOLS = "agent-tools";
export const AVAILABLE_TOOLS = "available-tools";
export const AGENT_OPTIONS = "agent-options";

export const agentsKey = (scope: QueryScope): QueryKey => queryKey(scope, AGENTS);
export const agentKey = (scope: QueryScope, id: string): QueryKey => queryKey(scope, AGENT, { id });
export const agentToolsKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, AGENT_TOOLS, { id });
export const availableToolsKey = (scope: QueryScope): QueryKey => queryKey(scope, AVAILABLE_TOOLS);
export const agentOptionsKey = (scope: QueryScope): QueryKey => queryKey(scope, AGENT_OPTIONS);
