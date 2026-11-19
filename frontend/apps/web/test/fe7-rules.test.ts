/**
 * FE-7's pure rules (ADR-201): who sees which control on /team (TM-03 - absent, not disabled), the
 * roles an invitation may carry, an invitation's state, the workspace PATCH (only changed keys),
 * the closing confirmation, closing the account with exactly one proof, the owned workspaces of an
 * `account_owns_workspaces` refusal, the activity log's words and paging, the language's one-time
 * reconciliation (LC-06, OQ-2) and the Google flow word kept in the tab (SG-02).
 */
import { describe, expect, it } from "vitest";

import { noteGoogleFlow, takeGoogleFlow } from "../lib/account/google-flow";
import { alreadyReconciled, markReconciled, reconcileLocale } from "../lib/account/locale";
import { changesOf, hasWords, mergePages, nextUntil, WORDED_ACTIONS } from "../lib/team/activity";
import {
  activeOwners,
  closingBody,
  confirmationMatches,
  invitableRoles,
  invitationState,
  memberControls,
  ownedWorkspaces,
  workspacePatch,
  type MemberFacts,
  type TenantRole,
  type WorkspaceDraft,
} from "../lib/team/rules";

const row = (userId: string, role: TenantRole, status = "active"): MemberFacts => ({
  userId,
  role,
  status,
});

describe("the role-to-control table (TM-03)", () => {
  const viewers: TenantRole[] = ["tenant_owner", "tenant_admin", "member"];
  const rows: TenantRole[] = ["tenant_owner", "tenant_admin", "member"];

  it("a member gets no control on anybody else's row", () => {
    for (const target of rows) {
      const controls = memberControls({ userId: "me", role: "member" }, row("x", target), 2);
      expect(Object.values(controls).some(Boolean)).toBe(false);
    }
  });

  it("an administrator never sees Remove on an owner's row (M-Q25)", () => {
    const controls = memberControls(
      { userId: "me", role: "tenant_admin" },
      row("x", "tenant_owner"),
      2,
    );
    expect(controls.remove).toBe(false);
    expect(
      memberControls({ userId: "me", role: "tenant_admin" }, row("x", "member"), 1).remove,
    ).toBe(true);
  });

  it("nobody sees Remove on the last active owner's row (M-Q26)", () => {
    for (const viewer of viewers) {
      expect(
        memberControls({ userId: "me", role: viewer }, row("x", "tenant_owner"), 1).remove,
      ).toBe(false);
    }
    expect(
      memberControls({ userId: "me", role: "tenant_owner" }, row("x", "tenant_owner"), 2).remove,
    ).toBe(true);
  });

  it("only the owner sees Make owner, and never on an owner (M-Q27)", () => {
    expect(
      memberControls({ userId: "me", role: "tenant_admin" }, row("x", "member"), 1).makeOwner,
    ).toBe(false);
    expect(
      memberControls({ userId: "me", role: "tenant_owner" }, row("x", "tenant_admin"), 1).makeOwner,
    ).toBe(true);
    expect(
      memberControls({ userId: "me", role: "tenant_owner" }, row("x", "tenant_owner"), 2).makeOwner,
    ).toBe(false);
  });

  it("anyone may leave; the last owner is told to hand over instead", () => {
    for (const viewer of viewers) {
      const own = memberControls({ userId: "me", role: viewer }, row("me", viewer), 2);
      expect(own).toMatchObject({ leave: true, leaveBlocked: false, remove: false });
    }
    const last = memberControls(
      { userId: "me", role: "tenant_owner" },
      row("me", "tenant_owner"),
      1,
    );
    expect(last).toMatchObject({ leave: false, leaveBlocked: true });
  });

  it("a removed person can be reinstated by an owner or administrator only", () => {
    const gone = row("x", "member", "revoked");
    expect(memberControls({ userId: "me", role: "tenant_owner" }, gone, 1).reinstate).toBe(true);
    expect(memberControls({ userId: "me", role: "tenant_admin" }, gone, 1).reinstate).toBe(true);
    expect(memberControls({ userId: "me", role: "member" }, gone, 1).reinstate).toBe(false);
    expect(memberControls({ userId: "me", role: "tenant_owner" }, gone, 1).remove).toBe(false);
  });

  it("no role (not signed into this workspace) → nothing", () => {
    expect(
      Object.values(memberControls({ userId: "me", role: null }, row("x", "member"), 1)).some(
        Boolean,
      ),
    ).toBe(false);
  });

  it("counts only active owners", () => {
    expect(
      activeOwners([
        row("a", "tenant_owner"),
        row("b", "tenant_owner", "revoked"),
        row("c", "tenant_admin"),
      ]),
    ).toBe(1);
  });
});

describe("invitations", () => {
  it("an administrator cannot invite an owner; a member invites nobody", () => {
    expect(invitableRoles("tenant_owner")).toContain("tenant_owner");
    expect(invitableRoles("tenant_admin")).not.toContain("tenant_owner");
    expect(invitableRoles("member")).toEqual([]);
    expect(invitableRoles(null)).toEqual([]);
  });

  it("a pending invitation past its moment reads as expired, with an injected clock", () => {
    const now = Date.parse("2026-11-19T12:00:00Z");
    expect(invitationState({ status: "pending", expires_at: "2026-11-20T12:00:00Z" }, now)).toBe(
      "pending",
    );
    expect(invitationState({ status: "pending", expires_at: "2026-11-19T11:59:59Z" }, now)).toBe(
      "expired",
    );
    expect(invitationState({ status: "expired", expires_at: "2026-11-20T00:00:00Z" }, now)).toBe(
      "expired",
    );
    expect(invitationState({ status: "accepted", expires_at: "2026-11-20T00:00:00Z" }, now)).toBe(
      "other",
    );
  });
});

describe("the workspace PATCH sends only what changed (SW-03, M-Q32 / M-Q33)", () => {
  const initial: WorkspaceDraft = {
    name: "ABC Finishing",
    slug: "abc-finishing",
    timezone: "Africa/Cairo",
    disclosureEn: "",
    disclosureAr: "",
  };

  it("nothing changed → an empty body", () => {
    expect(
      workspacePatch(initial, { ...initial, name: " ABC Finishing " }, { owner: true }),
    ).toEqual({});
  });

  it("one field changed → that key alone", () => {
    expect(
      workspacePatch(initial, { ...initial, timezone: "Europe/Paris" }, { owner: true }),
    ).toEqual({ timezone: "Europe/Paris" });
  });

  it("an administrator never sends the address, even if the field moved", () => {
    expect(workspacePatch(initial, { ...initial, slug: "other" }, { owner: false })).toEqual({});
    expect(workspacePatch(initial, { ...initial, slug: "Other " }, { owner: true })).toEqual({
      slug: "other",
    });
  });

  it("the disclosure is sent whole; empty restores Wasla's own wording", () => {
    const withText = { ...initial, disclosureEn: "An assistant answers." };
    expect(workspacePatch(initial, withText, { owner: false })).toEqual({
      automation_disclosure: { en: "An assistant answers." },
    });
    expect(workspacePatch(withText, initial, { owner: false })).toEqual({
      automation_disclosure: {},
    });
  });
});

describe("closing (CW-02, AC-05)", () => {
  it("the typed address must match exactly (M-Q29)", () => {
    expect(confirmationMatches("abc-finishing", "abc-finishing")).toBe(true);
    expect(confirmationMatches("ABC-finishing", "abc-finishing")).toBe(false);
    expect(confirmationMatches("abc-finishing ", "abc-finishing")).toBe(false);
    expect(confirmationMatches("", "abc-finishing")).toBe(false);
  });

  it("the account closes with exactly one proof (M-Q30)", () => {
    expect(closingBody({ kind: "password", password: "pw" })).toEqual({ current_password: "pw" });
    expect(closingBody({ kind: "google" })).toBeNull();
  });

  it("account_owns_workspaces names exactly the workspaces it carries (M-Q31)", () => {
    expect(
      ownedWorkspaces({
        workspaces: [{ id: "1", name: "ABC", slug: "abc" }, { id: 2, name: "bad" }, "nonsense"],
      }),
    ).toEqual([{ id: "1", name: "ABC", slug: "abc" }]);
    expect(ownedWorkspaces(null)).toEqual([]);
    expect(ownedWorkspaces({ workspaces: "x" })).toEqual([]);
  });
});

describe("the activity log (AL-01 / AL-02)", () => {
  it("knows its worded actions and nothing else", () => {
    expect(WORDED_ACTIONS.length).toBe(new Set(WORDED_ACTIONS).size);
    expect(hasWords("member_invited")).toBe(true);
    expect(hasWords('<img src=x onerror="alert(1)">')).toBe(false);
  });

  it("shows before / after only for the fields it names", () => {
    expect(
      changesOf({ role: "tenant_admin", previous_role: "member", secret: "x", plan: "pro" }),
    ).toEqual([{ field: "role", from: "member", to: "tenant_admin" }]);
    expect(changesOf({ name: "New" })).toEqual([{ field: "name", from: null, to: "New" }]);
    expect(changesOf(null)).toEqual([]);
  });

  it("pages by the last row's moment and never repeats a row", () => {
    expect(nextUntil([])).toBeNull();
    expect(nextUntil([{ occurred_at: "b" }, { occurred_at: "a" }])).toBe("a");
    expect(
      mergePages([
        [{ id: "1" }, { id: "2" }],
        [{ id: "2" }, { id: "3" }],
      ]).map((r) => r.id),
    ).toEqual(["1", "2", "3"]);
  });
});

describe("the language follows the account (LC-06, OQ-2)", () => {
  it("an account without a language takes the device's, once", () => {
    expect(reconcileLocale(null, "ar")).toEqual({ kind: "write-account", locale: "ar" });
  });

  it("a device follows the account's language when they differ", () => {
    expect(reconcileLocale("ar", "fr")).toEqual({ kind: "follow-account", locale: "ar" });
  });

  it("agreeing, or an unknown value, does nothing", () => {
    expect(reconcileLocale("tr", "tr")).toEqual({ kind: "none" });
    expect(reconcileLocale("de", "en")).toEqual({ kind: "none" });
    expect(reconcileLocale("ar", "xx")).toEqual({ kind: "none" });
  });

  it("runs once per tab (M-Q35)", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    };
    expect(alreadyReconciled(storage)).toBe(false);
    markReconciled(storage);
    expect(alreadyReconciled(storage)).toBe(true);
    expect([...store.values()]).toEqual(["1"]);
    expect(alreadyReconciled(null)).toBe(false);
  });
});

describe("the Google flow word (SG-02)", () => {
  it("holds only link or reauth, and is taken once", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    noteGoogleFlow(storage, "reauth");
    expect([...store.values()]).toEqual(["reauth"]);
    expect(takeGoogleFlow(storage)).toBe("reauth");
    expect(takeGoogleFlow(storage)).toBeNull();
    store.set("wasla_google_flow", "token-like-value");
    expect(takeGoogleFlow(storage)).toBeNull();
    expect(store.size).toBe(0);
  });
});
