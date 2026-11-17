"use client";

/**
 * The customers composites (FE-5, UI5-02, ADR-199): presentational only - no data fetching, no
 * session, no router. Every name, number, note, tag and field value they draw is customer or
 * member text: rendered as React text inside `<bdi>` / `dir="auto"`, never as markup, and a link
 * inside a note is a button that asks first (FE-4's InertLink). Words come from the app, already
 * translated; a status is always a word, never colour alone.
 */
import { BadgeCheck, Check, ChevronDown, Copy, Pencil, type LucideIcon } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { Button } from "../primitives/button";
import { IconButton } from "../primitives/icon-button";
import { Input, Textarea } from "../primitives/input";
import { DropdownMenu } from "../primitives/menu";
import { focusRing } from "../primitives/styles";
import { InertLink } from "./inbox";

export type RenderLink = (props: {
  href: string;
  className: string;
  children: ReactNode;
}) => ReactNode;

export function RowLink({
  href,
  className,
  children,
  renderLink,
  marker,
}: {
  href: string;
  className: string;
  children: ReactNode;
  renderLink?: RenderLink | undefined;
  marker: Record<string, string>;
}) {
  if (renderLink) return <>{renderLink({ href, className, children, ...marker })}</>;
  return (
    <a href={href} className={className} {...marker}>
      {children}
    </a>
  );
}

export const rowClass = cn(
  "flex min-h-target w-full flex-col gap-1 rounded-lg px-4 py-3 text-start text-text no-underline",
  "hover:bg-secondary-subtle",
  focusRing,
);

/* ------------------------------------------------------------------ lists */

/** A named list of records with what follows it ("Load more", a status line). */
export function RecordList({
  label,
  children,
  footer,
  busy = false,
}: {
  label: string;
  children: ReactNode;
  footer?: ReactNode;
  busy?: boolean;
}) {
  return (
    <div className="flex flex-col gap-3" data-record-list="">
      <ul
        aria-label={label}
        aria-busy={busy || undefined}
        className="m-0 flex list-none flex-col gap-1 p-0"
      >
        {children}
      </ul>
      {footer ? <div className="flex justify-center">{footer}</div> : null}
    </div>
  );
}

export interface ContactRowProps {
  href: string;
  /** The name to show, else the number, else the app's "Unnamed customer" (text). */
  who: string;
  /** The number under the name, when the name is the first line. */
  handle?: string | null;
  /** The contact's open lead: its name (customer text) and its status as a badge. */
  leadName?: string | null;
  leadStatus?: ReactNode;
  /** The opted-out badge, already words; absent when the customer receives messages. */
  consent?: ReactNode;
  time?: string | null;
  timeFull?: string | null;
  /** State words read after the visible text, for screen readers. */
  stateLabel?: string;
  renderLink?: RenderLink;
}

/** One customer in the contacts list: who, their number, their lead, their consent, last seen. */
export function ContactRow({
  href,
  who,
  handle,
  leadName,
  leadStatus,
  consent,
  time,
  timeFull,
  stateLabel,
  renderLink,
}: ContactRowProps) {
  const descriptionId = useId();
  return (
    <li data-contact-row="">
      <RowLink
        href={href}
        className={rowClass}
        renderLink={renderLink}
        marker={{ "data-row-link": href }}
      >
        <span className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate text-md font-semibold">
            <bdi>{who}</bdi>
          </span>
          {time ? (
            <>
              <time className="shrink-0 text-xs text-text-muted" aria-describedby={descriptionId}>
                {time}
              </time>
              <span id={descriptionId} className="sr-only">
                {timeFull}
              </span>
            </>
          ) : null}
        </span>
        {handle ? (
          <span className="truncate text-sm text-text-muted">
            <bdi dir="ltr">{handle}</bdi>
          </span>
        ) : null}
        {leadName || leadStatus || consent ? (
          <span className="flex flex-wrap items-center gap-2">
            {leadName ? (
              <span className="min-w-0 truncate text-sm text-text-muted">
                <bdi>{leadName}</bdi>
              </span>
            ) : null}
            {leadStatus}
            {consent}
          </span>
        ) : null}
        {stateLabel ? <span className="sr-only">{stateLabel}</span> : null}
      </RowLink>
    </li>
  );
}

export interface LeadRowProps {
  href: string;
  /** The lead's name, else the customer's, else the app's "Unnamed lead" (text). */
  who: string;
  /** The status as a badge with its word. */
  status: ReactNode;
  /** The interest: one line of customer or member text. */
  interest?: string | null;
  /** Score, owner, budget, source - each already words. */
  facts?: Array<{ key: string; label: string; value: ReactNode }>;
  time?: string | null;
  timeFull?: string | null;
  stateLabel?: string;
  renderLink?: RenderLink;
}

/** One lead in a list: who, where it stands, what it is about, and its facts. */
export function LeadRow({
  href,
  who,
  status,
  interest,
  facts,
  time,
  timeFull,
  stateLabel,
  renderLink,
}: LeadRowProps) {
  const descriptionId = useId();
  return (
    <li data-lead-row="">
      <RowLink
        href={href}
        className={rowClass}
        renderLink={renderLink}
        marker={{ "data-row-link": href }}
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-md font-semibold">
            <bdi>{who}</bdi>
          </span>
          {status}
          {time ? (
            <>
              <time className="shrink-0 text-xs text-text-muted" aria-describedby={descriptionId}>
                {time}
              </time>
              <span id={descriptionId} className="sr-only">
                {timeFull}
              </span>
            </>
          ) : null}
        </span>
        {interest ? (
          <span className="truncate text-sm text-text-muted">
            <bdi dir="auto">{interest}</bdi>
          </span>
        ) : null}
        {facts?.length ? (
          <span className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-text-muted">
            {facts.map((fact) => (
              <span key={fact.key} data-fact={fact.key}>
                {fact.label} <span className="font-medium text-text">{fact.value}</span>
              </span>
            ))}
          </span>
        ) : null}
        {stateLabel ? <span className="sr-only">{stateLabel}</span> : null}
      </RowLink>
    </li>
  );
}

/** The band above a list holding its search and filters (the secondary role). */
export function FilterBar({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div
      role="group"
      aria-label={label}
      data-filter-bar=""
      className="flex flex-col gap-4 rounded-xl bg-secondary-subtle p-4 sm:p-5"
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ a record's head */

/** A record page's head: the name as the h1, what stands beside it, its facts and its actions. */
export function RecordHeader({
  title,
  titleId,
  beside,
  sub,
  facts,
  actions,
}: {
  /** The record's name (customer text): drawn in `<bdi>`. */
  title: string;
  titleId?: string;
  /** What sits beside the name: a status badge, an "Edit name" control. */
  beside?: ReactNode;
  /** A quiet line under the name. */
  sub?: ReactNode;
  facts?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header
      className="flex flex-col gap-4 rounded-xl bg-secondary-subtle p-5 sm:p-6"
      data-record-header=""
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="flex min-w-0 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <h1
              id={titleId}
              tabIndex={-1}
              className="m-0 min-w-0 break-words text-3xl font-semibold leading-tight tracking-tight text-text outline-none"
            >
              <bdi>{title}</bdi>
            </h1>
            {beside}
          </div>
          {sub ? <div className="text-md text-text-muted">{sub}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap gap-3">{actions}</div> : null}
      </div>
      {facts}
    </header>
  );
}

/**
 * A record's facts and judgements - how it is known, its score, its owner - on the secondary
 * surface: with the header band, the 30 % of a record page. Forms, notes and the timeline stay on
 * white cards.
 */
export function RecordPanel({
  children,
  ...rest
}: {
  children: ReactNode;
} & Record<`data-${string}` | `aria-${string}`, string | undefined>) {
  return (
    <section className="rounded-xl bg-secondary-subtle p-6 sm:p-8" data-record-panel="" {...rest}>
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------ inline edit */

export interface InlineEditFieldProps {
  /** The field's name, read with it ("Name"). */
  label: string;
  /** The current value: what the field starts from. */
  value: string;
  /** The control that opens the field ("Edit name"). */
  editLabel: string;
  saveLabel: string;
  cancelLabel: string;
  maxLength: number;
  /** "12 / 200", in the page's digits. */
  counter: (length: number, max: number) => string;
  /** A translated error for the last save, shown under the field. */
  error?: ReactNode;
  /** Saves; resolving closes the field, rejecting keeps it open with `error`. */
  onSave: (value: string) => Promise<unknown>;
  /** Called when the field opens or is cancelled, so the app can clear its error. */
  onReset?: () => void;
  /** What is shown at rest beside the edit control, when the heading does not already show it. */
  children?: ReactNode;
}

/**
 * A value edited in place: "Edit" swaps the text for a field without motion; Enter saves, Escape
 * cancels, and focus returns to the control that opened it. Nothing is sent while it is unchanged.
 */
export function InlineEditField({
  label,
  value,
  editLabel,
  saveLabel,
  cancelLabel,
  maxLength,
  counter,
  error,
  onSave,
  onReset,
  children,
}: InlineEditFieldProps) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState(value);
  const openerId = `${id}-open`;
  const returnFocus = useRef(false);
  const fieldElement = () => document.getElementById(id) as HTMLInputElement | null;

  useEffect(() => {
    if (editing) {
      fieldElement()?.focus();
      fieldElement()?.select();
    } else if (returnFocus.current) {
      returnFocus.current = false;
      document.getElementById(openerId)?.focus();
    }
  }, [editing]); // eslint-disable-line react-hooks/exhaustive-deps

  const close = () => {
    returnFocus.current = true;
    setEditing(false);
  };
  const cancel = () => {
    onReset?.();
    close();
  };
  const save = async () => {
    const next = draft.trim();
    if (saving) return;
    if (next === value.trim()) {
      cancel();
      return;
    }
    setSaving(true);
    try {
      await onSave(next);
      close();
    } catch {
      fieldElement()?.focus();
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <span className="inline-flex flex-wrap items-center gap-2" data-inline-edit="view">
        {children}
        <Button
          id={openerId}
          variant="ghost"
          onClick={() => {
            onReset?.();
            setDraft(value);
            setEditing(true);
          }}
        >
          <Icon icon={Pencil} name="Pencil" size={16} />
          {editLabel}
        </Button>
      </span>
    );
  }
  const errorId = error ? `${id}-error` : undefined;
  return (
    <form
      className="flex w-full max-w-prose flex-col gap-2"
      data-inline-edit={saving ? "saving" : error ? "error" : "editing"}
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void save();
      }}
    >
      <label htmlFor={id} className="text-sm font-medium text-text">
        {label}
      </label>
      <Input
        id={id}
        value={draft}
        maxLength={maxLength}
        autoComplete="off"
        dir="auto"
        aria-invalid={error ? true : undefined}
        aria-describedby={[errorId, `${id}-count`].filter(Boolean).join(" ")}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
          if (event.key === "Escape") {
            event.preventDefault();
            cancel();
          }
        }}
      />
      <p id={`${id}-count`} className="m-0 text-sm text-text-muted">
        <bdi>{counter(draft.length, maxLength)}</bdi>
      </p>
      {error ? (
        <p id={errorId} role="alert" className="m-0 text-sm font-medium text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-3">
        <Button type="submit" variant="primary" busy={saving}>
          {saveLabel}
        </Button>
        <Button type="button" variant="secondary" disabled={saving} onClick={cancel}>
          {cancelLabel}
        </Button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ identities and consent */

export interface IdentityItem {
  key: string;
  /** "WhatsApp". */
  channel: string;
  /** "Phone number", "WhatsApp user id". */
  kind: string;
  /** The value as the channel reported it: drawn left to right in every locale. */
  value: string;
  copyLabel: string;
}

/** Every way a channel knows this customer; each value can be copied, never edited. */
export function IdentityList({
  label,
  items,
  onCopy,
}: {
  label: string;
  items: IdentityItem[];
  onCopy: (value: string) => void;
}) {
  return (
    <ul aria-label={label} className="m-0 flex list-none flex-col gap-2 p-0" data-identities="">
      {items.map((item) => (
        <li
          key={item.key}
          className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-border pb-2 last:border-b-0 last:pb-0"
        >
          <span className="flex min-w-0 flex-col">
            <span className="text-sm text-text-muted">
              {item.channel} · {item.kind}
            </span>
            <span className="break-all text-md text-text">
              <bdi dir="ltr">{item.value}</bdi>
            </span>
          </span>
          <IconButton
            icon={Copy}
            iconName="Copy"
            label={item.copyLabel}
            onClick={() => onCopy(item.value)}
          />
        </li>
      ))}
    </ul>
  );
}

export interface ConsentEntry {
  key: string;
  /** One sentence: what happened and who did it ("Opted out by the customer"). */
  what: ReactNode;
  /** The channel, a word. */
  channel: string;
  time: string;
  timeFull: string;
}

/** The record of every opt-out and every clear, newest first. */
export function ConsentHistory({
  label,
  entries,
  empty,
  footer,
}: {
  label: string;
  entries: ConsentEntry[];
  /** What the list says when nothing was ever recorded. */
  empty: string;
  footer?: ReactNode;
}) {
  if (!entries.length) {
    return (
      <p className="m-0 text-sm text-text-muted" data-consent-history="empty">
        {empty}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-consent-history="">
      <ol aria-label={label} className="m-0 flex list-none flex-col gap-3 p-0">
        {entries.map((entry) => (
          <li
            key={entry.key}
            className="flex flex-col gap-1 border-b border-border pb-3 last:border-b-0 last:pb-0"
          >
            <span className="text-md text-text">{entry.what}</span>
            <span className="text-sm text-text-muted">
              {entry.channel} · <time title={entry.timeFull}>{entry.time}</time>
            </span>
          </li>
        ))}
      </ol>
      {footer ? <div className="flex justify-start">{footer}</div> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ status, score, marks */

export interface StatusMove {
  value: string;
  label: string;
}

/**
 * Where a lead stands, and the moves it is given - it never knows the pipeline's graph (LG-01).
 * With no move to offer it draws no menu at all, only the words the app passes for that.
 */
export function StatusMenu({
  current,
  moves,
  changeLabel,
  closedLabel,
  busy = false,
  onMove,
}: {
  /** The current status, as a badge with its word. */
  current: ReactNode;
  moves: StatusMove[];
  /** The trigger's words ("Change status"). */
  changeLabel: string;
  /** What is said in place of the menu when no move exists ("Won - closed"). */
  closedLabel?: string | null;
  busy?: boolean;
  onMove: (value: string) => void;
}) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-status-menu="">
      {current}
      {moves.length ? (
        <DropdownMenu
          trigger={
            <Button variant="secondary" busy={busy} data-status-trigger="">
              {changeLabel}
              <Icon icon={ChevronDown} name="ChevronDown" size={16} />
            </Button>
          }
          items={moves.map((move) => ({
            type: "item" as const,
            key: move.value,
            label: move.label,
            onSelect: () => onMove(move.value),
          }))}
        />
      ) : closedLabel ? (
        <span className="text-sm text-text-muted" data-status-closed="">
          {closedLabel}
        </span>
      ) : null}
    </span>
  );
}

/** A whole number between two bounds, saved on its own; a value outside is never sent. */
export function ScoreField({
  label,
  hint,
  value,
  min,
  max,
  saveLabel,
  busy = false,
  error,
  onSave,
}: {
  label: string;
  hint?: ReactNode;
  value: number;
  min: number;
  max: number;
  saveLabel: string;
  busy?: boolean;
  error?: ReactNode;
  onSave: (value: number) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const parsed = /^\d{1,3}$/.test(draft.trim()) ? Number(draft.trim()) : null;
  const valid = parsed !== null && parsed >= min && parsed <= max;
  const changed = valid && parsed !== value;
  return (
    <form
      className="flex flex-col gap-2"
      data-score-field=""
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (changed && !busy && parsed !== null) onSave(parsed);
      }}
    >
      <label htmlFor={id} className="text-sm font-medium text-text">
        {label}
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <Input
          id={id}
          className="max-w-32"
          inputMode="numeric"
          dir="ltr"
          autoComplete="off"
          value={draft}
          maxLength={3}
          aria-invalid={!valid || error ? true : undefined}
          aria-describedby={`${id}-hint`}
          onChange={(event) => setDraft(event.target.value.replace(/[^\d]/g, ""))}
        />
        <Button type="submit" variant="secondary" busy={busy} disabled={!changed}>
          {saveLabel}
        </Button>
      </div>
      <p
        id={`${id}-hint`}
        className={cn("m-0 text-sm", error ? "font-medium text-danger" : "text-text-muted")}
      >
        {error ?? hint}
      </p>
    </form>
  );
}

/** "Confirmed by a person": a field the AI will not overwrite - a mark and its words. */
export function FieldConfirmedMark({ label }: { label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 text-sm font-medium text-text"
      data-field-confirmed=""
    >
      <Icon icon={BadgeCheck} name="BadgeCheck" size={16} className="text-primary" />
      {label}
    </span>
  );
}

/* ------------------------------------------------------------------ notes and the timeline */

export type NotePart = { kind: "text"; text: string } | { kind: "link"; text: string; url: string };

export interface NoteItem {
  key: string;
  /** A member's name, or the app's word for the AI. */
  author: string;
  time: string;
  timeFull: string;
  /** The note split into text and link candidates by the app's tested matcher. */
  parts: NotePart[];
}

/** Notes as plain text with their line breaks; a link asks before it opens. No edit, no delete. */
export function NoteList({
  label,
  notes,
  empty,
  footer,
  onLink,
}: {
  label: string;
  notes: NoteItem[];
  empty: string;
  footer?: ReactNode;
  onLink: (url: string) => void;
}) {
  if (!notes.length) {
    return (
      <p className="m-0 text-sm text-text-muted" data-notes="empty">
        {empty}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-notes="">
      <ol aria-label={label} className="m-0 flex list-none flex-col gap-4 p-0">
        {notes.map((note) => (
          <li
            key={note.key}
            data-note=""
            className="flex flex-col gap-1 border-b border-border pb-4 last:border-b-0 last:pb-0"
          >
            <span className="text-sm text-text-muted">
              <span className="font-medium text-text">
                <bdi>{note.author}</bdi>
              </span>{" "}
              · <time title={note.timeFull}>{note.time}</time>
            </span>
            <p
              dir="auto"
              className="m-0 whitespace-pre-wrap break-words text-md text-text"
              data-note-text=""
            >
              {note.parts.map((part, index) =>
                part.kind === "link" ? (
                  <InertLink key={index} onActivate={() => onLink(part.url)}>
                    {part.text}
                  </InertLink>
                ) : (
                  <span key={index}>{part.text}</span>
                ),
              )}
            </p>
          </li>
        ))}
      </ol>
      {footer ? <div className="flex justify-start">{footer}</div> : null}
    </div>
  );
}

/** The field a note is written in: Ctrl / Cmd + Enter or the button adds it; empty is never sent. */
export function NoteComposer({
  label,
  hint,
  value,
  maxLength,
  counter,
  submitLabel,
  busy = false,
  error,
  onChange,
  onSubmit,
}: {
  label: string;
  hint?: ReactNode;
  value: string;
  maxLength: number;
  counter: (length: number, max: number) => string;
  submitLabel: string;
  busy?: boolean;
  error?: ReactNode;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const id = useId();
  const sendable = value.trim().length > 0 && value.length <= maxLength && !busy;
  return (
    <form
      className="flex flex-col gap-2"
      data-note-composer=""
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (sendable) onSubmit();
      }}
    >
      <label htmlFor={id} className="text-sm font-medium text-text">
        {label}
      </label>
      <Textarea
        id={id}
        rows={3}
        dir="auto"
        value={value}
        maxLength={maxLength}
        aria-describedby={`${id}-hint`}
        aria-invalid={error ? true : undefined}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && sendable) {
            event.preventDefault();
            onSubmit();
          }
        }}
      />
      <div
        id={`${id}-hint`}
        className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1"
      >
        <p className={cn("m-0 text-sm", error ? "font-medium text-danger" : "text-text-muted")}>
          {error ?? hint}
        </p>
        <p className="m-0 text-sm text-text-muted">
          <bdi>{counter(value.length, maxLength)}</bdi>
        </p>
      </div>
      <div className="flex justify-start">
        <Button type="submit" variant="secondary" busy={busy} disabled={!sendable}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

export interface TimelineEntry {
  key: string;
  icon?: LucideIcon;
  iconName?: string;
  /** One sentence: who did what, with any value inside it already isolated. */
  what: ReactNode;
  time: string;
  timeFull: string;
}

/** What happened to a record, in the order the API gives it. Nothing here animates. */
export function Timeline({
  label,
  entries,
  empty,
  footer,
}: {
  label: string;
  entries: TimelineEntry[];
  empty: string;
  footer?: ReactNode;
}) {
  if (!entries.length) {
    return (
      <p className="m-0 text-sm text-text-muted" data-timeline="empty">
        {empty}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-timeline="">
      <ol aria-label={label} className="m-0 flex list-none flex-col gap-4 p-0">
        {entries.map((entry) => (
          <li key={entry.key} data-timeline-entry="" className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="mt-1 inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-secondary-subtle text-text"
            >
              <Icon icon={entry.icon ?? Check} name={entry.iconName ?? "Check"} size={14} />
            </span>
            <span className="flex min-w-0 flex-col gap-1">
              <span className="break-words text-md text-text">{entry.what}</span>
              <time className="text-sm text-text-muted" title={entry.timeFull}>
                {entry.time}
              </time>
            </span>
          </li>
        ))}
      </ol>
      {footer ? <div className="flex justify-start">{footer}</div> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ the preview's notice */

/** A preview build with no server behind it says so, on the sign-in page's look (FEF-26). */
export function PreviewNotice({
  title,
  body,
  action,
}: {
  title: string;
  body: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="flex w-full flex-col gap-6" data-preview-notice="">
      <div className="flex flex-col gap-3">
        <h1 className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
          {title}
        </h1>
        <div className="m-0 text-md text-text-muted">{body}</div>
      </div>
      {action ? <div className="flex flex-wrap gap-3">{action}</div> : null}
    </section>
  );
}
