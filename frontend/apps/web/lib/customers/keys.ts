/**
 * The customers screens' query resources (plan section 21: `[wsId, sessionGen, resource, params]`).
 * A switch or a sign-out bumps the generation, so nothing of one workspace or session is read by
 * another. A search term is part of a key in memory only; no key is ever persisted.
 */
import { queryKey, type QueryKey, type QueryScope } from "@wasla/api-client";

export const CONTACTS = "contacts";
export const CONTACT = "contact";
export const CONSENT_EVENTS = "contact-consent-events";
export const CONTACT_CONVERSATIONS = "contact-conversations";
export const CONTACT_LEADS = "contact-leads";
export const LEADS = "leads";
export const LEAD_STATISTICS = "lead-statistics";
export const LEAD_NOTES = "lead-notes";
export const LEAD_ACTIVITY = "lead-activity";

export const contactKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, CONTACT, { id });
export const consentEventsKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, CONSENT_EVENTS, { id });
export const contactConversationsKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, CONTACT_CONVERSATIONS, { id });
export const contactLeadsKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, CONTACT_LEADS, { id });
export const leadStatisticsKey = (scope: QueryScope): QueryKey => queryKey(scope, LEAD_STATISTICS);
export const leadNotesKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, LEAD_NOTES, { id });
export const leadActivityKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, LEAD_ACTIVITY, { id });

/** Every loaded query of a resource of this workspace and session. */
export const resourcePrefix = (scope: QueryScope, resource: string) =>
  [scope.workspaceId, scope.sessionGeneration, resource] as const;
