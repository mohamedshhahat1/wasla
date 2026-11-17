/**
 * The navigation registry (FE-3, SH-03): only routes that exist. A later stage adds its entry
 * here when it builds the screen - never a placeholder, a "coming soon" item or a disabled link.
 * A unit test holds every route to a page in app/**.
 */
import {
  BookOpenText,
  Bot,
  Contact,
  House,
  Inbox,
  RadioTower,
  Target,
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
