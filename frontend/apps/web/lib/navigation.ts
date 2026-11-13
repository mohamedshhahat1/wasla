/**
 * The navigation registry (FE-3, SH-03): only routes that exist. A later stage adds its entry
 * here when it builds the screen - never a placeholder, a "coming soon" item or a disabled link.
 * A unit test holds every route to a page in app/**.
 */
import { House, RadioTower, type LucideIcon } from "lucide-react";

export type TenantRole = "tenant_owner" | "tenant_admin" | "member";

export const ALL_ROLES: readonly TenantRole[] = ["tenant_owner", "tenant_admin", "member"];

export interface NavEntry {
  key: string;
  /** The route; a request path whose first segment matches it makes the entry current. */
  route: string;
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
    key: "channels",
    route: "/channels",
    labelKey: "channels",
    icon: RadioTower,
    iconName: "RadioTower",
    roles: ALL_ROLES,
    page: "app/(app)/channels/page.tsx",
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
  return NAVIGATION.find((entry) => entry.route !== "/" && entry.route === first)?.key ?? null;
}
