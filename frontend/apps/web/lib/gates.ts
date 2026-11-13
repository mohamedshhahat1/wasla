/**
 * The workspace gates as one pure rule (FE-3, GT-09): facts in, the full state and the banners
 * out, in precedence order. The shell renders only what this returns; no component decides a gate
 * on its own. A table test walks every combination.
 *
 * Sources (GT-01): /auth/me (the workspace's status, the role), GET /workspace/gates, the AI
 * allowance from GET /billing/entitlements, the codes seen on any call and the stream's reasons.
 * Never an amount: nothing here carries one.
 */
import type { TenantRole } from "./navigation";

export type SubscriptionStatus =
  "trialing" | "active" | "past_due" | "suspended" | "cancelled" | "expired";

export interface WorkspaceGatesFacts {
  status: "active" | "suspended";
  payment_required: boolean;
  subscription_status: SubscriptionStatus | null;
  trial_ends_at: string | null;
  channel_capacity: { over_limit: boolean; reduction_pending: boolean };
}

export interface AllowanceFacts {
  limit: number | null;
  remaining: number | null;
  over_limit: boolean;
}

/** The live-updates condition the coordinator reports, reduced to what a banner needs. */
export type LiveCondition = "ok" | "retrying" | "opened-elsewhere" | "stopped";

export interface GateFacts {
  role: TenantRole | null;
  /** The workspace's status in /auth/me as last loaded. */
  profileStatus: "active" | "suspended" | null;
  /** workspace_suspended on a call, or the stream closed with workspace_unavailable. */
  suspendedSignal: boolean;
  /** membership_revoked on a call or as the stream's reason. */
  revokedSignal: boolean;
  gates: WorkspaceGatesFacts | null;
  allowance: AllowanceFacts | null;
  live: LiveCondition;
  /** Milliseconds since the epoch, injected (never the wall clock inside the rule). */
  now: number;
  /** Banners put away for this session (memory only). */
  dismissed: ReadonlySet<BannerKey>;
}

export type FullState = "revoked" | "suspended";

export type BannerKey =
  "payment" | "billing-member" | "ended" | "capacity" | "trial" | "allowance" | "live";

export type BannerTone = "danger" | "warning" | "info";

export interface GateBanner {
  key: BannerKey;
  tone: BannerTone;
  dismissible: boolean;
  /** capacity: which of the two; trial: whole days left; live: the condition. */
  detail?: "over" | "reduction" | LiveCondition;
  days?: number;
}

export interface GateDecision {
  fullState: FullState | null;
  banners: GateBanner[];
}

/** GT-09's order: payment / billing issue > capacity > trial ending > allowance > live updates. */
export const BANNER_ORDER: readonly BannerKey[] = [
  "payment",
  "billing-member",
  "ended",
  "capacity",
  "trial",
  "allowance",
  "live",
];

const DAY_MS = 86_400_000;
export const TRIAL_WARNING_DAYS = 7;

function isManager(role: TenantRole | null): boolean {
  return role === "tenant_owner" || role === "tenant_admin";
}

/** Whole days until `endsAt` (0 = later today, or already ended); null when not within the window. */
export function trialDaysLeft(endsAt: string | null, now: number): number | null {
  if (!endsAt) return null;
  const end = Date.parse(endsAt);
  if (Number.isNaN(end) || end < now) return null;
  const days = Math.floor((end - now) / DAY_MS);
  return days <= TRIAL_WARNING_DAYS ? days : null;
}

/** GT-05: the billing banner for one role, from /workspace/gates alone. */
function billingBanners(facts: GateFacts): GateBanner[] {
  const gates = facts.gates;
  if (!gates) return [];
  const status = gates.subscription_status;
  const manager = isManager(facts.role);
  if (gates.payment_required || status === "past_due" || status === "suspended") {
    return manager
      ? [{ key: "payment", tone: "danger", dismissible: false }]
      : [{ key: "billing-member", tone: "warning", dismissible: false }];
  }
  if (status === "cancelled" || status === "expired") {
    return manager ? [{ key: "ended", tone: "warning", dismissible: false }] : [];
  }
  if (status === "trialing" && manager) {
    const days = trialDaysLeft(gates.trial_ends_at, facts.now);
    if (days !== null) return [{ key: "trial", tone: "info", dismissible: true, days }];
  }
  return [];
}

/** GT-06: owners and admins hear about capacity in the shell; everyone sees it on /channels. */
function capacityBanners(facts: GateFacts): GateBanner[] {
  const capacity = facts.gates?.channel_capacity;
  if (!capacity || !isManager(facts.role)) return [];
  if (capacity.over_limit)
    return [{ key: "capacity", tone: "warning", dismissible: false, detail: "over" }];
  if (capacity.reduction_pending)
    return [{ key: "capacity", tone: "warning", dismissible: false, detail: "reduction" }];
  return [];
}

/** GT-07: the AI allowance used up - owners and admins; members get a line on home instead. */
export function allowanceExhausted(allowance: AllowanceFacts | null): boolean {
  if (!allowance || allowance.limit === null) return false;
  return allowance.over_limit || allowance.remaining === 0;
}

function allowanceBanners(facts: GateFacts): GateBanner[] {
  if (!isManager(facts.role) || !allowanceExhausted(facts.allowance)) return [];
  return [{ key: "allowance", tone: "warning", dismissible: true }];
}

/** GT-08: the stream's condition, for everyone. */
function liveBanners(facts: GateFacts): GateBanner[] {
  switch (facts.live) {
    case "ok":
      return [];
    case "retrying":
      return [{ key: "live", tone: "info", dismissible: false, detail: "retrying" }];
    case "opened-elsewhere":
      return [{ key: "live", tone: "info", dismissible: false, detail: "opened-elsewhere" }];
    case "stopped":
      return [{ key: "live", tone: "warning", dismissible: false, detail: "stopped" }];
  }
}

export function decideGates(facts: GateFacts): GateDecision {
  if (facts.revokedSignal) return { fullState: "revoked", banners: [] };
  const suspended =
    facts.suspendedSignal ||
    facts.profileStatus === "suspended" ||
    facts.gates?.status === "suspended";
  if (suspended) return { fullState: "suspended", banners: [] };
  const banners = [
    ...billingBanners(facts),
    ...capacityBanners(facts),
    ...allowanceBanners(facts),
    ...liveBanners(facts),
  ]
    .filter((banner) => !(banner.dismissible && facts.dismissed.has(banner.key)))
    .sort((a, b) => BANNER_ORDER.indexOf(a.key) - BANNER_ORDER.indexOf(b.key));
  return { fullState: null, banners };
}

/** The error codes that are gate signals (GT-02): one of them on any response re-reads the gates. */
export const GATE_SIGNAL_CODES = new Set([
  "workspace_suspended",
  "membership_revoked",
  "payment_required",
  "plan_limit_exceeded",
  "channel_capacity_exceeded",
  "channel_type_not_allowed",
]);

export type GateSignal = "revoked" | "suspended" | "recheck";

/** GT-01 / GT-02: what one error code on any call means for the gates (null: not a gate code). */
export function gateSignalOf(code: string | null): GateSignal | null {
  if (!code || !GATE_SIGNAL_CODES.has(code)) return null;
  if (code === "membership_revoked") return "revoked";
  if (code === "workspace_suspended") return "suspended";
  return "recheck";
}

/**
 * GT-02: how /workspace/gates and /billing/entitlements are read - once on entering a workspace,
 * then only when a gate signal invalidates them. Never on a mount, a focus or a reconnect, never
 * polled, so a later screen that also observes them adds no request.
 */
export const GATE_QUERY_POLICY = {
  staleTime: Infinity,
  refetchOnMount: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
  refetchInterval: false,
  retry: false,
} as const;
