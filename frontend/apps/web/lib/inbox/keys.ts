/**
 * The inbox's query resources (plan section 21: `[wsId, sessionGen, resource, params]`). A switch or
 * a sign-out bumps the generation, so nothing of one workspace or session is read by another.
 */
import { queryKey, type QueryKey, type QueryScope } from "@wasla/api-client";

export const INBOX = "inbox";
export const CONVERSATION = "conversation";
export const MESSAGES = "messages";
export const LEAD = "lead";
export const MEMBERS = "workspace-members";
export const CONNECTIONS = "channel-connections";
export const TEMPLATES = "templates";
export const UNREAD_SUMMARY = "unread-summary";

export const conversationKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, CONVERSATION, { id });
export const messagesKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, MESSAGES, { id });
export const leadKey = (scope: QueryScope, id: string): QueryKey => queryKey(scope, LEAD, { id });
export const membersKey = (scope: QueryScope): QueryKey => queryKey(scope, MEMBERS);
export const connectionsKey = (scope: QueryScope): QueryKey => queryKey(scope, CONNECTIONS);
export const templatesKey = (scope: QueryScope): QueryKey => queryKey(scope, TEMPLATES);
export const unreadSummaryKey = (scope: QueryScope): QueryKey => queryKey(scope, UNREAD_SUMMARY);

/** The prefix of every loaded inbox view of this workspace and session. */
export const inboxPrefix = (scope: QueryScope) =>
  [scope.workspaceId, scope.sessionGeneration, INBOX] as const;
