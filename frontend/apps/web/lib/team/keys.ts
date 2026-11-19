/**
 * FE-7's query resources (plan section 21: `[wsId, sessionGen, resource, params]`). The members
 * list keeps FE-4's "members" resource, so the inbox's names, the checklist's team step and the
 * team page read and invalidate one family; the prefix (no params) invalidates every variant.
 */
import { queryKey, type QueryKey, type QueryScope } from "@wasla/api-client";

import { MEMBERS } from "../inbox/keys";

export const INVITATIONS = "invitations";
export const WORKSPACE = "workspace";
export const TIMEZONES = "timezones";
export const ACTIVITY = "audit-logs";

export const teamMembersKey = (scope: QueryScope, left: boolean): QueryKey =>
  queryKey(scope, MEMBERS, left ? { include_revoked: true } : {});
/** Every members variant (with and without people who left) at once. */
export const membersFamily = (scope: QueryScope): readonly [string, number, string] => [
  scope.workspaceId,
  scope.sessionGeneration,
  MEMBERS,
];
export const invitationsKey = (scope: QueryScope): QueryKey => queryKey(scope, INVITATIONS);
export const workspaceKey = (scope: QueryScope): QueryKey => queryKey(scope, WORKSPACE);
export const timezonesKey = (scope: QueryScope): QueryKey => queryKey(scope, TIMEZONES);
export const activityKey = (scope: QueryScope): QueryKey => queryKey(scope, ACTIVITY);
