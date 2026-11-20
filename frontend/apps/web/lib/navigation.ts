/**
 * The navigation registry (FE-3, SH-03): only routes that exist. A later stage adds its entry
 * here when it builds the screen - never a placeholder, a "coming soon" item or a disabled link.
 * A unit test holds every route to a page in app/**.
 */
import {
  BookOpenText,
  Bot,
  Contact,
  CreditCard,
  House,
  Inbox,
  RadioTower,
  Settings,
  Target,
  UsersRound,
  type LucideIcon,
} from "lucide-react";

export type TenantRole = "tenant_owner" | "tenant_admin" | "member";

export const ALL_ROLES: readonly TenantRole[] = ["tenant_owner", "tenant_admin", "member"];
/** Knowledge is managed by owners and administrators (plan APP-KB-01, OQ-3). */
export const ADMIN_ROLES: readonly TenantRole[] = ["tenant_owner", "tenant_admin"];

export interface NavEntry {
  key: string;
  /** The route the entry links to. */
  route: string;
  /** The first path segment that makes the entry current, when it is not the route's own. */
  match?: string;
  /** The key under app.nav.* */
  labelKey: string;
  icon: LucideIcon;
  iconName: string;
  roles: readonly TenantRole[];
  /** The page file under app/ that serves the route (for the existence test). */
  page: string;
}

export const NAVIGATION: readonly NavEntry[] = [
  {
    key: "home",
    route: "/",
    labelKey: "home",
    icon: House,
    iconName: "House",
    roles: ALL_ROLES,
    page: "app/(app)/page.tsx",
  },
  {
    key: "inbox",
    route: "/inbox",
    labelKey: "inbox",
    icon: Inbox,
    iconName: "Inbox",
    roles: ALL_ROLES,
    page: "app/(app)/inbox/page.tsx",
  },
  {
    key: "contacts",
    route: "/contacts",
    labelKey: "contacts",
    icon: Contact,
    iconName: "Contact",
    roles: ALL_ROLES,
    page: "app/(app)/contacts/page.tsx",
  },
  {
    key: "leads",
    route: "/leads",
    labelKey: "leads",
    icon: Target,
    iconName: "Target",
    roles: ALL_ROLES,
    page: "app/(app)/leads/page.tsx",
  },
  {
    key: "channels",
    route: "/channels",
    labelKey: "channels",
    icon: RadioTower,
    iconName: "RadioTower",
    roles: ALL_ROLES,
    page: "app/(app)/channels/page.tsx",
  },
  {
    key: "ai",
    route: "/ai/agents",
    match: "/ai",
    labelKey: "ai",
    icon: Bot,
    iconName: "Bot",
    roles: ALL_ROLES,
    page: "app/(app)/ai/agents/page.tsx",
  },
  {
    key: "knowledge",
    route: "/knowledge",
    labelKey: "knowledge",
    icon: BookOpenText,
    iconName: "BookOpenText",
    roles: ADMIN_ROLES,
    page: "app/(app)/knowledge/page.tsx",
  },
  {
    // FE-7 (HM7-01): every member reads the team; the controls follow the API's rules.
    key: "team",
    route: "/team",
    labelKey: "team",
    icon: UsersRound,
    iconName: "UsersRound",
    roles: ALL_ROLES,
    page: "app/(app)/team/page.tsx",
  },
  {
    // The workspace's settings are an owner's or administrator's; the account page is in the
    // account menu for everybody.
    key: "settings",
    route: "/settings/workspace",
    match: "/settings",
    labelKey: "settings",
    icon: Settings,
    iconName: "Settings",
    roles: ADMIN_ROLES,
    page: "app/(app)/settings/workspace/page.tsx",
  },
  {
    // FE-8 (BL-03, OQ-5): the owner acts; an administrator reads the plan, usage and capacity; a
    // member has no billing (the payment banner tells them to ask the owner).
    key: "billing",
    route: "/billing",
    labelKey: "billing",
    icon: CreditCard,
    iconName: "CreditCard",
    roles: ADMIN_ROLES,
    page: "app/(app)/billing/page.tsx",
  },
];

/** The entries a role sees. */
export function entriesFor(role: string | null | undefined): NavEntry[] {
  return NAVIGATION.filter((entry) => entry.roles.includes(role as TenantRole));
}

/** The entry a path belongs to: "/" only for "/", others by their first segment. */
export function currentEntry(pathname: string): string | null {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return "home";
  const first = `/${path.split("/")[1] ?? ""}`;
  return (
    NAVIGATION.find((entry) => entry.route !== "/" && (entry.match ?? entry.route) === first)
      ?.key ?? null
  );
}
