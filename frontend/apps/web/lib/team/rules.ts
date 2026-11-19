/**
 * FE-7's team rules as pure functions (TM-03, TI-01, SW-02, AL-01, CW-01, AC-05; ADR-201): who may
 * do what on the members and invitations pages, read from the API's own rules - an administrator
 * never removes an owner, nobody removes or leaves as the last active owner, only the owner hands
 * over ownership or closes the workspace. A control a role may not use is absent, never disabled.
 */

export type TenantRole = "tenant_owner" | "tenant_admin" | "member";

export interface MemberFacts {
  userId: string;
  role: TenantRole;
  /** "active" or "revoked" today; any other value the API adds is read as not active. */
  status: string;
}

export interface MemberControls {
  remove: boolean;
  leave: boolean;
  /** The person's own row while they are the last active owner: told to hand over first. */
  leaveBlocked: boolean;
  reinstate: boolean;
  makeOwner: boolean;
}

export const NO_CONTROLS: MemberControls = {
  remove: false,
  leave: false,
  leaveBlocked: false,
  reinstate: false,
  makeOwner: false,
};

export function isAdminRole(role: string | null | undefined): boolean {
  return role === "tenant_owner" || role === "tenant_admin";
}

/** The active owners of a list - the "last owner" rule's count. */
export function activeOwners(members: readonly MemberFacts[]): number {
  return members.filter((m) => m.status === "active" && m.role === "tenant_owner").length;
}

/** The controls one viewer gets on one row (TM-03). */
export function memberControls(
  viewer: { userId: string; role: TenantRole | null },
  row: MemberFacts,
  owners: number,
): MemberControls {
  if (!viewer.role) return NO_CONTROLS;
  const own = row.userId === viewer.userId;
  const lastOwner = row.status === "active" && row.role === "tenant_owner" && owners <= 1;
  if (row.status !== "active") {
    return { ...NO_CONTROLS, reinstate: !own && isAdminRole(viewer.role) };
  }
  if (own) {
    return { ...NO_CONTROLS, leave: !lastOwner, leaveBlocked: lastOwner };
  }
  const mayRemove =
    isAdminRole(viewer.role) &&
    !lastOwner &&
    !(viewer.role === "tenant_admin" && row.role === "tenant_owner");
  return {
    ...NO_CONTROLS,
    remove: mayRemove,
    makeOwner: viewer.role === "tenant_owner" && row.role !== "tenant_owner",
  };
}

/** The roles an invitation may carry from this inviter (the API refuses an admin inviting an owner). */
export function invitableRoles(inviter: TenantRole | null): TenantRole[] {
  if (inviter === "tenant_owner") return ["member", "tenant_admin", "tenant_owner"];
  if (inviter === "tenant_admin") return ["member", "tenant_admin"];
  return [];
}

/** An invitation's state in words: pending until its moment, expired after (TI-02). */
export function invitationState(
  invitation: { status: string; expires_at: string },
  now: number,
): "pending" | "expired" | "other" {
  if (invitation.status !== "pending") return invitation.status === "expired" ? "expired" : "other";
  return Date.parse(invitation.expires_at) > now ? "pending" : "expired";
}

/** The workspace fields a PATCH sends: only the ones that changed (SW-03, FE-5's dirty-field rule). */
export interface WorkspaceDraft {
  name: string;
  slug: string;
  timezone: string;
  disclosureEn: string;
  disclosureAr: string;
}

export function workspacePatch(
  initial: WorkspaceDraft,
  current: WorkspaceDraft,
  options: { owner: boolean },
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  if (current.name.trim() !== initial.name.trim()) patch["name"] = current.name.trim();
  // A slug is ASCII by its pattern (ADR-047), so lowering it with a fixed locale is exact.
  const slugOf = (draft: WorkspaceDraft) => draft.slug.trim().toLocaleLowerCase("en-US");
  if (options.owner && slugOf(current) !== slugOf(initial)) patch["slug"] = slugOf(current);
  if (current.timezone !== initial.timezone) patch["timezone"] = current.timezone;
  if (
    current.disclosureEn.trim() !== initial.disclosureEn.trim() ||
    current.disclosureAr.trim() !== initial.disclosureAr.trim()
  ) {
    const wording: Record<string, string> = {};
    if (current.disclosureEn.trim()) wording["en"] = current.disclosureEn.trim();
    if (current.disclosureAr.trim()) wording["ar"] = current.disclosureAr.trim();
    // `{}` restores Wasla's own wording (OMNI-041).
    patch["automation_disclosure"] = wording;
  }
  return patch;
}

/** The closing confirmation matches only the address exactly as the API checks it (CW-02). */
export function confirmationMatches(typed: string, slug: string): boolean {
  return typed === slug;
}

/** Closing the account takes exactly one proof (AC-05): a password, or Google's - never both. */
export type ClosingProof = { kind: "password"; password: string } | { kind: "google" };

export function closingBody(proof: ClosingProof): { current_password: string } | null {
  return proof.kind === "password" ? { current_password: proof.password } : null;
}

/** The workspaces an `account_owns_workspaces` refusal names, from its details. */
export function ownedWorkspaces(
  details: Record<string, unknown> | null | undefined,
): Array<{ id: string; name: string; slug: string }> {
  const listed = details?.["workspaces"];
  if (!Array.isArray(listed)) return [];
  return listed.flatMap((item: unknown) => {
    const entry = item as { id?: unknown; name?: unknown; slug?: unknown };
    return typeof entry.id === "string" &&
      typeof entry.name === "string" &&
      typeof entry.slug === "string"
      ? [{ id: entry.id, name: entry.name, slug: entry.slug }]
      : [];
  });
}

/** The page after this session's workspace is gone (left, closed): the next one or none (LW). */
export type AfterLeaving = "next-workspace" | "no-workspace";
