/**
 * The query-key factory (PD-06, AC-04): `[wsId, sessionGen, resource, params]`. A workspace
 * switch or a sign-out bumps the session generation, so no cached answer of one workspace or
 * one session can ever be read by another - by construction, not by care.
 */

export type QueryParams = Readonly<Record<string, string | number | boolean | null | undefined>>;

export type QueryKey = readonly [
  workspaceId: string,
  sessionGeneration: number,
  resource: string,
  params: QueryParams,
];

export interface QueryScope {
  workspaceId: string;
  sessionGeneration: number;
}

function cleanParams(params: QueryParams | undefined): QueryParams {
  const entries = Object.entries(params ?? {})
    .filter(([, value]) => value !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return Object.freeze(Object.fromEntries(entries));
}

export function queryKey(scope: QueryScope, resource: string, params?: QueryParams): QueryKey {
  if (!scope.workspaceId) throw new Error("a query key needs a workspace id");
  if (!Number.isInteger(scope.sessionGeneration) || scope.sessionGeneration < 0) {
    throw new Error("a query key needs a session generation");
  }
  return Object.freeze([
    scope.workspaceId,
    scope.sessionGeneration,
    resource,
    cleanParams(params),
  ] as const);
}

/** The prefix that matches every key of one workspace in one session (for invalidation). */
export function scopePrefix(scope: QueryScope): readonly [string, number] {
  return Object.freeze([scope.workspaceId, scope.sessionGeneration] as const);
}
