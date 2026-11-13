"use client";

import {
  Check,
  ChevronDown,
  Circle,
  CircleAlert,
  CircleHelp,
  Info,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { IconButton } from "../primitives/icon-button";
import { focusRing } from "../primitives/styles";

/**
 * Status, data and checklist composites (FE-3, UI3-02). A status always has words and an icon,
 * never colour alone (WCAG 1.4.1); red, amber and green only for a status; information is blue.
 */

export type StatusTone = "danger" | "warning" | "info" | "success" | "neutral";

const toneIcon: Record<StatusTone, { icon: LucideIcon; name: string }> = {
  danger: { icon: CircleAlert, name: "CircleAlert" },
  warning: { icon: TriangleAlert, name: "TriangleAlert" },
  info: { icon: Info, name: "Info" },
  success: { icon: Check, name: "Check" },
  neutral: { icon: Circle, name: "Circle" },
};

const bannerTone: Record<Exclude<StatusTone, "neutral" | "success">, string> = {
  danger: "bg-danger-subtle",
  warning: "bg-warning-subtle",
  info: "bg-info-subtle",
};

const iconTone: Record<StatusTone, string> = {
  danger: "text-danger",
  warning: "text-warning",
  info: "text-info",
  success: "text-success",
  neutral: "text-text-muted",
};

export interface BannerProps {
  tone: "danger" | "warning" | "info";
  title: ReactNode;
  children?: ReactNode;
  /** One action (a Button or a link). */
  action?: ReactNode;
  /** A banner that may be put away for the session has this, with its button's name. */
  onDismiss?: () => void;
  dismissLabel?: string;
  /** Kept on the element for tests and the gate's own ordering. */
  name?: string;
}

/**
 * One line of state across the page, under the top bar: compact, so a status colour never
 * becomes the screen (VQ3-02: status ≤ 8 % on a gate screen).
 */
export function Banner({
  tone,
  title,
  children,
  action,
  onDismiss,
  dismissLabel,
  name,
}: BannerProps) {
  const icon = toneIcon[tone];
  return (
    <div
      data-banner={name ?? ""}
      data-tone={tone}
      className={cn(
        "flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-border px-4 py-3 sm:px-6 lg:px-8",
        bannerTone[tone],
      )}
    >
      <Icon icon={icon.icon} name={icon.name} className={cn("mt-1", iconTone[tone])} />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="m-0 text-md font-semibold text-text">{title}</p>
        {children ? <div className="m-0 text-sm text-text">{children}</div> : null}
      </div>
      {action ? <div className="flex items-center">{action}</div> : null}
      {onDismiss && dismissLabel ? (
        <IconButton icon={X} iconName="X" label={dismissLabel} onClick={onDismiss} />
      ) : null}
    </div>
  );
}

/**
 * The banner area (GT-09): a named region; at most two banners visible, the rest behind one
 * disclosure button that says how many there are.
 */
export function BannerStack({
  label,
  banners,
  moreLabel,
  fewerLabel,
}: {
  label: string;
  /** Already in precedence order. */
  banners: Array<{ key: string; node: ReactNode }>;
  moreLabel: (hidden: number) => string;
  fewerLabel: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();
  if (banners.length === 0) return null;
  const shown = expanded ? banners : banners.slice(0, 2);
  const hidden = banners.length - 2;
  return (
    <section aria-label={label} data-banner-stack="">
      <div id={panelId}>
        {shown.map((banner) => (
          <div key={banner.key}>{banner.node}</div>
        ))}
      </div>
      {hidden > 0 ? (
        <div className="flex border-b border-border bg-surface px-4 sm:px-6 lg:px-8">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={() => setExpanded((open) => !open)}
            className={cn(
              "inline-flex min-h-target cursor-pointer items-center gap-2 border-0 bg-transparent px-0",
              "text-sm font-medium text-primary underline underline-offset-4",
              focusRing,
            )}
          >
            <Icon
              icon={ChevronDown}
              name="ChevronDown"
              size={16}
              className={expanded ? "rotate-180" : undefined}
            />
            {expanded ? fewerLabel : moreLabel(hidden)}
          </button>
        </div>
      ) : null}
    </section>
  );
}

const badgeTone: Record<StatusTone, string> = {
  danger: "bg-danger-subtle text-danger",
  warning: "bg-warning-subtle text-warning",
  info: "bg-info-subtle text-info",
  success: "bg-success-subtle text-success",
  neutral: "bg-secondary-subtle text-text",
};

/** A small status pill: text and an icon, never colour alone. */
export function Badge({
  tone = "neutral",
  icon = true,
  children,
}: {
  tone?: StatusTone;
  icon?: boolean;
  children: ReactNode;
}) {
  const mark = toneIcon[tone];
  return (
    <span
      data-badge={tone}
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-3 py-1 text-xs font-medium",
        badgeTone[tone],
      )}
    >
      {icon ? <Icon icon={mark.icon} name={mark.name} size={14} /> : null}
      {children}
    </span>
  );
}

/** A label, a number and an optional sub-line, on the one card recipe. */
export function StatCard({
  label,
  value,
  sub,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  testId?: string;
}) {
  return (
    <div
      data-stat-card=""
      data-testid={testId}
      className="flex flex-col gap-1 rounded-xl border border-border bg-surface p-5 shadow-sm"
    >
      <p className="m-0 text-sm text-text-muted">{label}</p>
      <p className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
        <bdi>{value}</bdi>
      </p>
      {sub ? <p className="m-0 text-sm text-text-muted">{sub}</p> : null}
    </div>
  );
}

/**
 * Used out of a total, as text and a bar. role="meter" (WAI-ARIA 1.2,
 * https://www.w3.org/TR/wai-aria-1.2/#meter): a scalar within a known range, not a task's progress.
 * "Over" is said in words beside the bar, never by its colour alone.
 */
export function Meter({
  label,
  value,
  max,
  valueText,
  over = false,
  overLabel,
}: {
  label: string;
  value: number;
  max: number;
  /** The words a screen reader hears and the line under the bar shows ("2 of 3 in use"). */
  valueText: string;
  over?: boolean;
  overLabel?: string;
}) {
  const id = useId();
  const safeMax = max > 0 ? max : 1;
  const ratio = Math.max(0, Math.min(1, value / safeMax));
  return (
    <div className="flex flex-col gap-2" data-meter="">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span id={id} className="text-sm font-medium text-text">
          {label}
        </span>
        <span className="text-sm text-text-muted">
          <bdi>{valueText}</bdi>
        </span>
      </div>
      <div
        role="meter"
        aria-labelledby={id}
        aria-valuemin={0}
        aria-valuemax={safeMax}
        aria-valuenow={Math.min(value, safeMax)}
        aria-valuetext={valueText}
        className="h-3 w-full overflow-hidden rounded-full border border-border-strong bg-surface"
      >
        <div
          className={cn("h-full rounded-full", over ? "bg-warning" : "bg-primary")}
          style={{ inlineSize: `${Math.round(ratio * 100)}%` }}
        />
      </div>
      {over && overLabel ? <Badge tone="warning">{overLabel}</Badge> : null}
    </div>
  );
}

export type ChecklistState = "done" | "undone" | "unknown";

export interface ChecklistItem {
  key: string;
  label: ReactNode;
  state: ChecklistState;
  /** A short line under the label (what to do, or that it comes later). */
  detail?: ReactNode;
  action?: ReactNode;
}

const stepIcon: Record<ChecklistState, { icon: LucideIcon; name: string; tone: string }> = {
  done: { icon: Check, name: "Check", tone: "bg-success-subtle text-success" },
  undone: {
    icon: Circle,
    name: "Circle",
    tone: "bg-surface text-text-muted border border-border-strong",
  },
  unknown: { icon: CircleHelp, name: "CircleHelp", tone: "bg-warning-subtle text-warning" },
};

/** Steps with a state each, said in words beside the icon. */
export function Checklist({
  items,
  stateLabels,
}: {
  items: ChecklistItem[];
  stateLabels: Record<ChecklistState, string>;
}) {
  return (
    <ol className="m-0 flex list-none flex-col gap-3 p-0" data-checklist="">
      {items.map((item) => {
        const mark = stepIcon[item.state];
        return (
          <li
            key={item.key}
            data-step={item.key}
            data-state={item.state}
            className="flex flex-wrap items-start gap-3 rounded-lg border border-border bg-surface px-4 py-3"
          >
            <span
              className={cn(
                "inline-flex size-8 shrink-0 items-center justify-center rounded-full",
                mark.tone,
              )}
            >
              <Icon icon={mark.icon} name={mark.name} size={16} />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <p className="m-0 text-md font-medium text-text">
                {item.label}
                <span className="sr-only"> - {stateLabels[item.state]}</span>
              </p>
              {item.detail ? <p className="m-0 text-sm text-text-muted">{item.detail}</p> : null}
            </div>
            <span aria-hidden="true" className="text-sm text-text-muted">
              {stateLabels[item.state]}
            </span>
            {item.action ? <div className="flex w-full sm:w-auto">{item.action}</div> : null}
          </li>
        );
      })}
    </ol>
  );
}

/** Label / value rows (a connection's facts). */
export function KeyValueList({
  items,
}: {
  items: Array<{ key: string; label: ReactNode; value: ReactNode }>;
}) {
  return (
    <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2" data-key-values="">
      {items.map((item) => (
        <div key={item.key} className="flex min-w-0 flex-col gap-1" data-key={item.key}>
          <dt className="text-sm text-text-muted">{item.label}</dt>
          <dd className="m-0 break-words text-md text-text">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export type LiveState = "live" | "connecting" | "paused" | "down" | "off";

/**
 * The live-updates mark in the top bar (GT-08): a still dot and a word - nothing loops (CM-06).
 * The announcement is the app's (one polite message per change, not this mark).
 */
export function LiveIndicator({ state, label }: { state: LiveState; label: string }) {
  return (
    <span
      data-live-indicator={state}
      className="inline-flex min-h-target items-center gap-2 rounded-full px-3 text-sm text-on-brand"
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block size-2 rounded-full",
          state === "live" ? "bg-on-brand" : "border border-on-brand-muted bg-transparent",
        )}
      />
      <span className={state === "live" ? "sr-only sm:not-sr-only" : undefined}>{label}</span>
    </span>
  );
}

const stateBand: Record<"danger" | "warning" | "info", string> = {
  danger: "bg-danger-subtle text-danger",
  warning: "bg-warning-subtle text-warning",
  info: "bg-info-subtle text-info",
};

/** A full state in place of a page (suspended, access withdrawn): an icon band, a heading, words. */
export function StatusState({
  tone,
  title,
  description,
  action,
  headingId,
  className,
}: {
  tone: "danger" | "warning" | "info";
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  /** So the app can move focus to the heading when the state appears (GT-09). */
  headingId?: string;
  className?: string;
}) {
  const mark = toneIcon[tone];
  const band = stateBand[tone];
  return (
    <section
      data-status-state={tone}
      className={cn(
        "mx-auto flex w-full max-w-prose flex-col overflow-hidden rounded-xl border border-border bg-surface text-center shadow-sm",
        className,
      )}
    >
      <div aria-hidden="true" className="flex justify-center bg-secondary-subtle py-10">
        <span className={cn("inline-flex size-14 items-center justify-center rounded-full", band)}>
          <Icon icon={mark.icon} name={mark.name} size={28} />
        </span>
      </div>
      <div className="flex flex-col items-center gap-4 px-6 py-8 sm:px-10">
        <h1
          id={headingId}
          tabIndex={-1}
          className="m-0 text-2xl font-semibold leading-tight tracking-tight text-text outline-none"
        >
          {title}
        </h1>
        {description ? <div className="m-0 text-md text-text-muted">{description}</div> : null}
        {action ? <div className="mt-3 flex flex-wrap justify-center gap-3">{action}</div> : null}
      </div>
    </section>
  );
}
