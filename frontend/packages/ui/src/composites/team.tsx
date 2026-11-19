/**
 * FE-7's composites (UI7-02, ADR-201): the team, settings and account screens' presentational
 * parts. Presentational only - every word arrives as a prop, every action as a node the screen
 * builds, so a role that may not act gets no control (absent, not disabled). Role tokens only;
 * names and addresses in <bdi>; times passed in, never read from a clock here.
 */
import { CircleAlert, Clock, ShieldCheck, UserRound } from "lucide-react";
import { useId, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";

/* ----------------------------------------------------------------- roles */

export type RoleTone = "owner" | "admin" | "member";

const roleTone: Record<RoleTone, string> = {
  owner: "bg-primary text-on-brand",
  admin: "bg-secondary-subtle text-text",
  member: "bg-surface text-text-muted border border-border",
};

/** A role in words (Owner, Administrator, Member): the accent for the owner alone. */
export function RoleBadge({ tone, children }: { tone: RoleTone; children: ReactNode }) {
  return (
    <span
      data-role-badge={tone}
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-3 py-1 text-xs font-medium",
        roleTone[tone],
      )}
    >
      {tone === "owner" ? <Icon icon={ShieldCheck} name="ShieldCheck" size={14} /> : null}
      {children}
    </span>
  );
}

/* --------------------------------------------------------------- members */

export interface MemberRowProps {
  name: string;
  email: string;
  role: ReactNode;
  /** "You", on the viewer's own row. */
  youLabel?: string | null;
  /** "Joined 3 days ago", or "Left 2 days ago" for a removed person. */
  when: string | null;
  whenFull: string | null;
  /** A removed person's row is quieter (people who left). */
  removed?: boolean;
  /** The row's actions, built by the screen for the viewer's role: absent when none. */
  actions?: ReactNode;
  /** A sentence instead of an action (the last owner who cannot leave yet). */
  note?: ReactNode;
}

export function MemberRow({
  name,
  email,
  role,
  youLabel,
  when,
  whenFull,
  removed = false,
  actions,
  note,
}: MemberRowProps) {
  const whenId = useId();
  return (
    <li
      data-member-row=""
      data-removed={removed ? "" : undefined}
      className={cn(
        "flex flex-col gap-3 rounded-lg p-4 sm:flex-row sm:items-center sm:justify-between",
        removed ? "bg-surface-sunken" : "bg-surface",
      )}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span
          aria-hidden="true"
          className="mt-1 inline-flex size-9 shrink-0 items-center justify-center rounded-full bg-secondary-subtle text-text-muted"
        >
          <Icon icon={UserRound} name="UserRound" size={18} />
        </span>
        <div className="flex min-w-0 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "min-w-0 truncate text-md font-semibold",
                removed ? "text-text-muted" : "text-text",
              )}
            >
              <bdi>{name}</bdi>
            </span>
            {youLabel ? <span className="text-sm text-text-muted">({youLabel})</span> : null}
            {role}
          </span>
          <span className="truncate text-sm text-text-muted">
            <bdi dir="ltr">{email}</bdi>
          </span>
          {when ? (
            <span className="text-sm text-text-muted">
              <time aria-describedby={whenId}>{when}</time>
              <span id={whenId} className="sr-only">
                {whenFull}
              </span>
            </span>
          ) : null}
          {note ? <span className="text-sm text-text">{note}</span> : null}
        </div>
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">{actions}</div>
      ) : null}
    </li>
  );
}

/* ----------------------------------------------------------- invitations */

export interface InvitationRowProps {
  email: string;
  role: ReactNode;
  /** "Expires in 3 days" or "Expired". */
  state: string;
  expired: boolean;
  stateFull: string | null;
  /** The e-mail's language, in its own name ("العربية"). */
  language?: string | null;
  languageLabel?: string;
  actions?: ReactNode;
}

export function InvitationRow({
  email,
  role,
  state,
  expired,
  stateFull,
  language,
  languageLabel,
  actions,
}: InvitationRowProps) {
  const stateId = useId();
  return (
    <li
      data-invitation-row=""
      data-expired={expired ? "" : undefined}
      className="flex flex-col gap-3 rounded-lg bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="flex min-w-0 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 truncate text-md font-semibold text-text">
            <bdi dir="ltr">{email}</bdi>
          </span>
          {role}
        </span>
        <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-muted">
          <span
            className={cn("inline-flex items-center gap-1", expired && "font-medium text-text")}
            aria-describedby={stateFull ? stateId : undefined}
          >
            <Icon
              icon={expired ? CircleAlert : Clock}
              name={expired ? "CircleAlert" : "Clock"}
              size={14}
            />
            {state}
          </span>
          {stateFull ? (
            <span id={stateId} className="sr-only">
              {stateFull}
            </span>
          ) : null}
          {language ? (
            <span>
              {languageLabel} <span className="font-medium text-text">{language}</span>
            </span>
          ) : null}
        </span>
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </li>
  );
}

/* ---------------------------------------------------------- settings nav */

export interface SettingsNavItem {
  key: string;
  href: string;
  label: string;
}

/**
 * The settings area's own navigation: links at a desktop width (a list beside the page), and the
 * same links as a wrapped row on a phone - never a select, so every entry stays a real link.
 */
export function SettingsNav({
  label,
  items,
  current,
  renderLink,
}: {
  label: string;
  items: readonly SettingsNavItem[];
  current: string | null;
  renderLink: (props: {
    href: string;
    className: string;
    children: ReactNode;
    "aria-current"?: "page";
  }) => ReactNode;
}) {
  return (
    <nav aria-label={label} data-settings-nav="">
      <ul className="m-0 flex list-none flex-wrap gap-1 p-0 md:flex-col">
        {items.map((item) => {
          const active = item.key === current;
          return (
            <li key={item.key}>
              {renderLink({
                href: item.href,
                className: cn(
                  "flex min-h-target items-center rounded-md px-3 text-md no-underline focus-visible:outline-2 focus-visible:outline-offset-2",
                  active
                    ? "bg-secondary-subtle font-semibold text-text"
                    : "text-text-muted hover:bg-surface-sunken hover:text-text",
                ),
                children: item.label,
                ...(active ? { "aria-current": "page" as const } : {}),
              })}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/* -------------------------------------------------------------- activity */

export interface ActivityRowProps {
  /** Who: a name, "Wasla" for the system, or "a former member". */
  actor: string;
  /** What, in words - or the action's code as text when the catalogue does not know it. */
  action: string;
  /** True when `action` is the raw code. */
  actionIsCode?: boolean;
  /** On what (a member's name, an address), when the entry names one. */
  target?: string | null;
  when: string;
  whenFull: string;
  /** Before → after, only for the fields the screen knows how to say. */
  change?: ReactNode;
}

export function ActivityRow({
  actor,
  action,
  actionIsCode = false,
  target,
  when,
  whenFull,
  change,
}: ActivityRowProps) {
  const whenId = useId();
  return (
    <li
      data-activity-row=""
      className="flex flex-col gap-1 border-b border-border py-3 last:border-b-0"
    >
      <span className="flex flex-wrap items-baseline gap-x-2 text-md text-text">
        <span className="font-semibold">
          <bdi>{actor}</bdi>
        </span>
        {actionIsCode ? (
          <code dir="ltr" className="rounded-sm bg-surface-sunken px-1 text-sm">
            {action}
          </code>
        ) : (
          <span>{action}</span>
        )}
        {target ? (
          <span className="font-medium">
            <bdi>{target}</bdi>
          </span>
        ) : null}
      </span>
      {change ? <span className="text-sm text-text-muted">{change}</span> : null}
      <span className="text-sm text-text-muted">
        <time aria-describedby={whenId}>{when}</time>
        <span id={whenId} className="sr-only">
          {whenFull}
        </span>
      </span>
    </li>
  );
}

/* ------------------------------------------------------------ danger zone */

/** An irreversible action's section: what it does, in words, and its one button (UI7-01). */
export function DangerZone({
  title,
  headingLevel = 2,
  children,
  action,
}: {
  title: string;
  headingLevel?: 2 | 3;
  children: ReactNode;
  action?: ReactNode;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      data-danger-zone=""
      className="flex flex-col gap-3 rounded-lg border border-danger bg-surface p-5"
    >
      <Heading id={headingId} className="m-0 text-lg font-semibold text-text">
        {title}
      </Heading>
      <div className="flex flex-col gap-2 text-md text-text">{children}</div>
      {action ? <div className="flex flex-wrap gap-3">{action}</div> : null}
    </section>
  );
}
