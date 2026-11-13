/**
 * The channel screens' rules (FE-3, CN-02, CN-05): which connect adapters exist, and whether the
 * Connect action is offered - pure, with a table test.
 */
import type { TenantRole } from "./navigation";

export type ChannelType = "whatsapp" | "instagram" | "messenger";

export interface ConnectAdapter {
  /** The registry key and the route segment: /channels/connect/<key>. */
  key: string;
  channel: ChannelType;
  labelKey: string;
  roles: readonly TenantRole[];
}

/**
 * CN-02: a channel is connected by an adapter, not a screen family. FE-3 registers the assisted
 * WhatsApp form only; Embedded Signup (FEQ-14, the next backend stage) adds a "whatsapp-embedded"
 * entry beside it. No Instagram or Messenger adapter exists (LP-FE-02), so none is offered.
 */
export const CONNECT_ADAPTERS: readonly ConnectAdapter[] = [
  {
    key: "whatsapp",
    channel: "whatsapp",
    labelKey: "connectWhatsApp",
    roles: ["tenant_owner", "tenant_admin"],
  },
];

export type ConnectRefusal = "role" | "loading" | "noAdapter" | "notAllowed" | "noSlots";

export interface ConnectFacts {
  role: TenantRole | null;
  channel: ChannelType;
  /** null while the capacity is loading or could not be read. */
  capacity: {
    remaining: number | null;
    allowed_channel_types: readonly string[];
  } | null;
  adapters?: readonly ConnectAdapter[];
}

export type ConnectDecision =
  { allowed: true; adapter: ConnectAdapter } | { allowed: false; reason: ConnectRefusal };

export function connectDecision(facts: ConnectFacts): ConnectDecision {
  const adapter = (facts.adapters ?? CONNECT_ADAPTERS).find(
    (entry) => entry.channel === facts.channel,
  );
  if (facts.role !== "tenant_owner" && facts.role !== "tenant_admin") {
    return { allowed: false, reason: "role" };
  }
  if (!adapter || !adapter.roles.includes(facts.role))
    return { allowed: false, reason: "noAdapter" };
  if (!facts.capacity) return { allowed: false, reason: "loading" };
  if (!facts.capacity.allowed_channel_types.includes(facts.channel)) {
    return { allowed: false, reason: "notAllowed" };
  }
  // remaining null = no limit.
  if (facts.capacity.remaining !== null && facts.capacity.remaining <= 0) {
    return { allowed: false, reason: "noSlots" };
  }
  return { allowed: true, adapter };
}

/** Who may act on a connection (verify, disable, enable, release) and choose what stays. */
export function canManageChannels(role: TenantRole | null): boolean {
  return role === "tenant_owner" || role === "tenant_admin";
}

export function canChooseConnections(role: TenantRole | null): boolean {
  return role === "tenant_owner";
}

/** The last four digits of a display number, for the release confirmation (CN-06). */
export function lastFourDigits(displayNumber: string): string {
  const digits = displayNumber.replace(/\D/g, "");
  return digits.slice(-4);
}
