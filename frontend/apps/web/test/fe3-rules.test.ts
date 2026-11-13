/**
 * FE-3's pure rules: the gates (GT-05..GT-09), the checklist (HM-03), the connect action (CN-05),
 * the navigation registry (SH-03) and the stream handler table (ST-04) - each a table over every
 * combination that matters.
 */
import { existsSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { EVENT_TYPES, queryKey } from "@wasla/api-client";

import {
  canChooseConnections,
  canManageChannels,
  CONNECT_ADAPTERS,
  connectDecision,
  lastFourDigits,
} from "../lib/channels";
import {
  checklistLoading,
  checklistOpen,
  checklistStates,
  type ChecklistInputs,
} from "../lib/checklist";
import {
  allowanceExhausted,
  gateSignalOf,
  BANNER_ORDER,
  decideGates,
  GATE_QUERY_POLICY,
  trialDaysLeft,
  type GateFacts,
  type LiveCondition,
  type SubscriptionStatus,
} from "../lib/gates";
import {
  ALL_ROLES,
  currentEntry,
  entriesFor,
  NAVIGATION,
  type TenantRole,
} from "../lib/navigation";
import { FRAME_HANDLERS, handleFrame, HOME_CONVERSATIONS } from "../lib/stream-handlers";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = join(here, "..");
const NOW = Date.parse("2026-11-13T10:00:00Z");
const DAY = 86_400_000;

function facts(overrides: Partial<GateFacts> = {}): GateFacts {
  return {
    role: "tenant_owner",
    profileStatus: "active",
    suspendedSignal: false,
    revokedSignal: false,
    gates: {
      status: "active",
      payment_required: false,
      subscription_status: "active",
      trial_ends_at: null,
      channel_capacity: { over_limit: false, reduction_pending: false },
    },
    allowance: { limit: 100, remaining: 40, over_limit: false },
    live: "ok",
    now: NOW,
    dismissed: new Set(),
    ...overrides,
  };
}

const ROLES: TenantRole[] = ["tenant_owner", "tenant_admin", "member"];
const STATUSES: Array<SubscriptionStatus | null> = [
  null,
  "trialing",
  "active",
  "past_due",
  "suspended",
  "cancelled",
  "expired",
];

describe("GT-05: the billing banner per role × subscription × payment × trial window", () => {
  for (const role of ROLES) {
    for (const status of STATUSES) {
      for (const payment of [false, true]) {
        for (const trialIn of [null, 3, 30]) {
          const manager = role !== "member";
          it(`${role} ${status} payment=${payment} trial=${trialIn}`, () => {
            const decision = decideGates(
              facts({
                role,
                gates: {
                  status: "active",
                  payment_required: payment,
                  subscription_status: status,
                  trial_ends_at:
                    trialIn === null
                      ? null
                      : new Date(NOW + trialIn * DAY + 3600_000).toISOString(),
                  channel_capacity: { over_limit: false, reduction_pending: false },
                },
              }),
            );
            const keys = decision.banners.map((banner) => banner.key);
            const owed = payment || status === "past_due" || status === "suspended";
            if (owed) {
              expect(keys).toEqual([manager ? "payment" : "billing-member"]);
              expect(decision.banners[0]!.tone).toBe(manager ? "danger" : "warning");
              expect(decision.banners[0]!.dismissible).toBe(false);
            } else if (status === "cancelled" || status === "expired") {
              expect(keys).toEqual(manager ? ["ended"] : []);
            } else if (status === "trialing" && manager && trialIn === 3) {
              expect(keys).toEqual(["trial"]);
              expect(decision.banners[0]).toMatchObject({
                tone: "info",
                dismissible: true,
                days: 3,
              });
            } else {
              expect(keys).toEqual([]);
            }
            // A member never sees the owner's payment sentence (M-Z23).
            if (!manager) expect(keys).not.toContain("payment");
          });
        }
      }
    }
  }

  it("the trial window: 0..7 whole days ahead, none after it or once ended", () => {
    expect(trialDaysLeft(new Date(NOW + 2 * 3600_000).toISOString(), NOW)).toBe(0);
    expect(trialDaysLeft(new Date(NOW + 7 * DAY).toISOString(), NOW)).toBe(7);
    expect(trialDaysLeft(new Date(NOW + 8 * DAY).toISOString(), NOW)).toBeNull();
    expect(trialDaysLeft(new Date(NOW - 1000).toISOString(), NOW)).toBeNull();
    expect(trialDaysLeft(null, NOW)).toBeNull();
  });
});

describe("GT-06 / GT-07 / GT-08: capacity, allowance and live updates", () => {
  for (const role of ROLES) {
    const manager = role !== "member";
    for (const [over, reduction] of [
      [false, false],
      [true, false],
      [false, true],
      [true, true],
    ] as const) {
      it(`${role} capacity over=${over} reduction=${reduction}`, () => {
        const decision = decideGates(
          facts({
            role,
            gates: {
              ...facts().gates!,
              channel_capacity: { over_limit: over, reduction_pending: reduction },
            },
          }),
        );
        const banner = decision.banners.find((item) => item.key === "capacity");
        if (!manager || (!over && !reduction)) expect(banner).toBeUndefined();
        else expect(banner?.detail).toBe(over ? "over" : "reduction");
      });
    }
    for (const allowance of [
      { limit: 100, remaining: 0, over_limit: false },
      { limit: 100, remaining: 0, over_limit: true },
      { limit: 100, remaining: 5, over_limit: false },
      { limit: null, remaining: null, over_limit: false },
    ]) {
      it(`${role} allowance ${JSON.stringify(allowance)}`, () => {
        const decision = decideGates(facts({ role, allowance }));
        const shown = decision.banners.some((item) => item.key === "allowance");
        expect(shown).toBe(manager && allowance.limit !== null && allowance.remaining === 0);
        expect(allowanceExhausted(allowance)).toBe(
          allowance.limit !== null && allowance.remaining === 0,
        );
      });
    }
    for (const live of ["ok", "retrying", "opened-elsewhere", "stopped"] as LiveCondition[]) {
      it(`${role} live ${live}`, () => {
        const banner = decideGates(facts({ role, live })).banners.find(
          (item) => item.key === "live",
        );
        if (live === "ok") expect(banner).toBeUndefined();
        else expect(banner?.detail).toBe(live);
      });
    }
  }
});

describe("GT-09: one place decides; the full states win; the order holds", () => {
  it("revoked beats suspended beats every banner (M-Z11)", () => {
    const everything = facts({
      gates: {
        status: "suspended",
        payment_required: true,
        subscription_status: "past_due",
        trial_ends_at: null,
        channel_capacity: { over_limit: true, reduction_pending: true },
      },
      allowance: { limit: 1, remaining: 0, over_limit: true },
      live: "stopped",
    });
    expect(decideGates(everything)).toEqual({ fullState: "suspended", banners: [] });
    expect(decideGates({ ...everything, revokedSignal: true })).toEqual({
      fullState: "revoked",
      banners: [],
    });
    for (const source of [
      { suspendedSignal: true },
      { profileStatus: "suspended" as const },
      { gates: { ...facts().gates!, status: "suspended" as const } },
    ]) {
      expect(decideGates(facts(source)).fullState).toBe("suspended");
    }
  });

  it("banners come in BANNER_ORDER", () => {
    const decision = decideGates(
      facts({
        gates: {
          status: "active",
          payment_required: true,
          subscription_status: "past_due",
          trial_ends_at: null,
          channel_capacity: { over_limit: true, reduction_pending: false },
        },
        allowance: { limit: 10, remaining: 0, over_limit: false },
        live: "retrying",
      }),
    );
    expect(decision.banners.map((banner) => banner.key)).toEqual([
      "payment",
      "capacity",
      "allowance",
      "live",
    ]);
    const indices = decision.banners.map((banner) => BANNER_ORDER.indexOf(banner.key));
    expect([...indices].sort((a, b) => a - b)).toEqual(indices);
  });

  it("only a dismissible banner can be dismissed", () => {
    const base = facts({
      gates: { ...facts().gates!, payment_required: true },
      allowance: { limit: 10, remaining: 0, over_limit: false },
    });
    const dismissed = decideGates({ ...base, dismissed: new Set(["payment", "allowance"]) });
    expect(dismissed.banners.map((banner) => banner.key)).toEqual(["payment"]);
  });

  it("no gates read yet: no billing or capacity banner, never a guess", () => {
    expect(decideGates(facts({ gates: null, allowance: null })).banners).toEqual([]);
  });
});

describe("HM-03: the checklist from five reads", () => {
  const all: ChecklistInputs = {
    connections: [{ status: "active" }],
    agents: [{ is_default: true }],
    bases: [{}],
    members: [{ status: "active" }, { status: "active" }],
    subscriptionStatus: "active",
  };
  it("every step done closes the list", () => {
    const states = checklistStates(all);
    expect(Object.values(states)).toEqual(["done", "done", "done", "done", "done"]);
    expect(checklistOpen(states)).toBe(false);
  });
  it("a new workspace: every step to do", () => {
    const states = checklistStates({
      connections: [],
      agents: [],
      bases: [],
      members: [{ status: "active" }],
      subscriptionStatus: "none",
    });
    expect(Object.values(states)).toEqual(["undone", "undone", "undone", "undone", "undone"]);
    expect(checklistOpen(states)).toBe(true);
  });
  it("each step turns on its own fact", () => {
    expect(checklistStates({ ...all, connections: [{ status: "disabled" }] }).channel).toBe(
      "undone",
    );
    expect(checklistStates({ ...all, agents: [{ is_default: false }] }).agent).toBe("undone");
    expect(
      checklistStates({ ...all, members: [{ status: "active" }, { status: "revoked" }] }).team,
    ).toBe("undone");
  });
  for (const status of [
    "trialing",
    "active",
    "past_due",
    "suspended",
    "cancelled",
    "expired",
    "none",
  ] as const) {
    it(`plan step for ${status} (M-Z22: null / none is never done)`, () => {
      expect(checklistStates({ ...all, subscriptionStatus: status }).plan).toBe(
        status === "active" || status === "trialing" ? "done" : "undone",
      );
    });
  }
  it("while any read is loading the list claims no state", () => {
    expect(checklistLoading(all)).toBe(false);
    expect(checklistLoading({ ...all, bases: null })).toBe(true);
    expect(checklistLoading({ ...all, agents: "error" })).toBe(false);
  });
  it("a failed read is 'could not check', never done; loading likewise", () => {
    const states = checklistStates({ ...all, agents: "error", bases: null });
    expect(states.agent).toBe("unknown");
    expect(states.knowledge).toBe("unknown");
    expect(checklistOpen(states)).toBe(true);
  });
});

describe("CN-05: the connect action - role × remaining × allowed types × adapter", () => {
  for (const role of [...ROLES, null]) {
    for (const remaining of [null, 0, 1]) {
      for (const allowed of [["whatsapp"], ["instagram"]]) {
        for (const adapters of [CONNECT_ADAPTERS, []]) {
          it(`${role} remaining=${remaining} allowed=${allowed.join()} adapters=${adapters.length}`, () => {
            const decision = connectDecision({
              role: role,
              channel: "whatsapp",
              capacity: { remaining, allowed_channel_types: allowed },
              adapters,
            });
            const manager = role === "tenant_owner" || role === "tenant_admin";
            if (!manager) expect(decision).toEqual({ allowed: false, reason: "role" });
            else if (!adapters.length)
              expect(decision).toEqual({ allowed: false, reason: "noAdapter" });
            else if (!allowed.includes("whatsapp"))
              expect(decision).toEqual({ allowed: false, reason: "notAllowed" });
            else if (remaining === 0)
              expect(decision).toEqual({ allowed: false, reason: "noSlots" });
            else expect(decision.allowed).toBe(true);
          });
        }
      }
    }
  }
  it("capacity not read yet: not offered", () => {
    expect(connectDecision({ role: "tenant_owner", channel: "whatsapp", capacity: null })).toEqual({
      allowed: false,
      reason: "loading",
    });
  });
  it("who manages and who chooses (M-Z18)", () => {
    expect(ROLES.map(canManageChannels)).toEqual([true, true, false]);
    expect(ROLES.map(canChooseConnections)).toEqual([true, false, false]);
  });
  it("the release confirmation reads the last four digits", () => {
    expect(lastFourDigits("+20 100 000 1024")).toBe("1024");
    expect(lastFourDigits("15550001234")).toBe("1234");
  });
});

/** Every page file under app/ (route groups included), as paths relative to the app root. */
function pages(dir = join(appRoot, "app")): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return pages(path);
    return entry.name === "page.tsx" ? [relative(appRoot, path).replaceAll("\\", "/")] : [];
  });
}

/** The URL a page file serves: route groups dropped. */
function routeOf(page: string): string {
  const segments = page
    .replace(/^app\//, "")
    .replace(/\/?page\.tsx$/, "")
    .split("/")
    .filter((segment) => segment && !/^\(.*\)$/.test(segment));
  return `/${segments.join("/")}`;
}

describe("SH-03: the navigation goes only where a page exists (M-Z21)", () => {
  const served = new Set(pages().map(routeOf));
  for (const entry of NAVIGATION) {
    it(`${entry.key} → ${entry.route} has its page`, () => {
      expect(existsSync(join(appRoot, entry.page))).toBe(true);
      expect(routeOf(entry.page)).toBe(entry.route);
      expect(served.has(entry.route)).toBe(true);
    });
  }
  it("FE-3 registers exactly Home and Channels, for every role", () => {
    expect(NAVIGATION.map((entry) => entry.route)).toEqual(["/", "/channels"]);
    for (const role of ALL_ROLES)
      expect(entriesFor(role).map((entry) => entry.key)).toEqual(["home", "channels"]);
    expect(entriesFor(null)).toEqual([]);
  });
  it("the current entry follows the path", () => {
    expect(currentEntry("/")).toBe("home");
    expect(currentEntry("/channels")).toBe("channels");
    expect(currentEntry("/channels/abc")).toBe("channels");
    expect(currentEntry("/channels/connect/whatsapp")).toBe("channels");
    expect(currentEntry("/workspaces")).toBeNull();
  });
});

describe("ST-04: the handler table", () => {
  it("has an entry for every event type of the vocabulary and the control frames", () => {
    expect(Object.keys(FRAME_HANDLERS).sort()).toEqual([...EVENT_TYPES, "closed", "resync"].sort());
  });
  it("message.created invalidates home's conversations; the others change nothing", () => {
    const queryClient = new QueryClient();
    const scope = { workspaceId: "ws-1", sessionGeneration: 3 };
    const key = queryKey(scope, HOME_CONVERSATIONS);
    queryClient.setQueryData(key, { items: [] });
    for (const type of EVENT_TYPES.filter((item) => item !== "message.created")) {
      handleFrame({ type, id: "1-0", data: {} }, { queryClient, scope });
    }
    expect(queryClient.getQueryState(key)?.isInvalidated).toBe(false);
    handleFrame({ type: "message.created", id: "2-0", data: {} }, { queryClient, scope });
    expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
  });
});

describe("GT-01: an error code on any call as a gate signal (M-Z12)", () => {
  it("revoked, suspended, a re-read, or nothing", () => {
    expect(gateSignalOf("membership_revoked")).toBe("revoked");
    expect(gateSignalOf("workspace_suspended")).toBe("suspended");
    for (const code of [
      "payment_required",
      "plan_limit_exceeded",
      "channel_capacity_exceeded",
      "channel_type_not_allowed",
    ]) {
      expect(gateSignalOf(code)).toBe("recheck");
    }
    for (const code of ["permission_denied", "not_found", "conflict", null]) {
      expect(gateSignalOf(code)).toBeNull();
    }
  });
});

describe("GT-02: the gates are read once per workspace, then only on a signal (M-Z16)", () => {
  it("never on a mount, a focus, a reconnect or a timer; never stale by age", () => {
    expect(GATE_QUERY_POLICY).toEqual({
      staleTime: Infinity,
      refetchOnMount: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      refetchInterval: false,
      retry: false,
    });
  });
});
