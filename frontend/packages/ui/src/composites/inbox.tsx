"use client";

/**
 * The inbox's composites (FE-4, UI4-02). Presentational only: every word, number, handler and
 * piece of customer text comes from the app; nothing here calls the API, reads the session or
 * decides product rules. Customer text is drawn as React text only (TH-02, IL-07) - names, handles
 * and excerpts inside `<bdi>`, message text with `dir="auto"`.
 *
 * Roles (DESIGN_BRIEF_FE4, taste-skill): the list pane on the light-blue secondary band with the
 * selected row in white; inbound bubbles white with a hairline, outbound bubbles light blue; the
 * accent only for the view's one primary (Send) and the unread count.
 */

import {
  AlertCircle,
  ArrowDown,
  Check,
  CheckCheck,
  Clock,
  Download,
  FileText,
  Film,
  ImageOff,
  Mic,
  X,
  type LucideIcon,
} from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { forwardRef, useEffect, useId, useState, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { Button } from "../primitives/button";
import { Dialog } from "../primitives/dialog";
import { IconButton } from "../primitives/icon-button";
import { focusRing } from "../primitives/styles";

/* ------------------------------------------------------------------ the unread count */

/**
 * The count pill: Latin digits in `<bdi>`, already capped by the app ("99+"). The digits are
 * hidden from screen readers; `label` ("3 unread") is read instead, as part of the link it sits in.
 */
export function CountBadge({ value, label }: { value: string; label?: string | null }) {
  return (
    <>
      <span
        data-count=""
        aria-hidden="true"
        className="inline-flex min-w-6 items-center justify-center rounded-full bg-primary px-2 py-1 text-xs font-semibold leading-none text-on-primary"
      >
        <bdi>{value}</bdi>
      </span>
      {label ? <span className="sr-only">{label}</span> : null}
    </>
  );
}

/* ------------------------------------------------------------------ the view controls */

/**
 * One choice of a few, shown at once (the inbox's status views): a labelled group of toggle
 * buttons, the chosen one pressed and white on the light-blue band - never colour alone.
 */
export function SegmentedControl({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1" data-segmented="">
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(option.value)}
            className={cn(
              "wasla-press min-h-target rounded-md border px-4 text-sm",
              on
                ? "border-border-strong bg-surface font-medium text-text shadow-sm"
                : "border-transparent text-text hover:bg-surface",
              focusRing,
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ rows */

export interface ConversationRowProps {
  href: string;
  /** Who: a name, else the handle, else the app's "Unknown customer" (text). */
  who: string;
  /** The handle under the name when the name is shown. */
  handle?: string | null;
  channelIcon: LucideIcon;
  channelIconName: string;
  channelLabel: string;
  /** The connected number's label, when the workspace has more than one connection. */
  number?: string | null;
  /** The preview line: a prefix of ours ("Reply:", "Photo") and the excerpt (customer text). */
  previewPrefix?: string | null;
  previewText?: string | null;
  /** Relative time, with its full form as the accessible description. */
  time: string;
  timeFull: string;
  /** State badges (status, mode, priority, assignee), already words. */
  badges?: ReactNode;
  /** "12" / "99+", or null when nothing is unread. */
  unread?: string | null;
  unreadLabel?: string | null;
  selected?: boolean;
  /** State words read with the row's visible text ("Open, AI, 3 unread"), for screen readers. */
  stateLabel?: string;
  renderLink?: (props: {
    href: string;
    className: string;
    children: ReactNode;
    "aria-current"?: "page";
    "data-conversation-row": string;
  }) => ReactNode;
  /** The row's own menu (Mark as unread): a trigger the app renders. */
  menu?: ReactNode;
}

export function ConversationRow({
  href,
  who,
  handle,
  channelIcon,
  channelIconName,
  channelLabel,
  number,
  previewPrefix,
  previewText,
  time,
  timeFull,
  badges,
  unread,
  unreadLabel,
  selected = false,
  stateLabel,
  renderLink,
  menu,
}: ConversationRowProps) {
  const descriptionId = useId();
  const className = cn(
    "flex min-h-target w-full flex-col gap-1 rounded-lg px-4 py-3 text-start no-underline",
    selected ? "bg-surface text-text shadow-sm" : "text-text hover:bg-surface hover:shadow-sm",
    focusRing,
  );
  const body = (
    <>
      <span className="flex items-baseline gap-2">
        <Icon icon={channelIcon} name={channelIconName} size={16} label={channelLabel} />
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-md",
            unread ? "font-semibold" : "font-medium",
          )}
        >
          <bdi>{who}</bdi>
        </span>
        <time className="shrink-0 text-xs text-text-muted" aria-describedby={descriptionId}>
          {time}
        </time>
        <span id={descriptionId} className="sr-only">
          {timeFull}
        </span>
      </span>
      {handle ? (
        <span className="truncate text-sm text-text-muted">
          <bdi dir="ltr">{handle}</bdi>
          {number ? (
            <>
              {" · "}
              <bdi>{number}</bdi>
            </>
          ) : null}
        </span>
      ) : number ? (
        <span className="truncate text-sm text-text-muted">
          <bdi>{number}</bdi>
        </span>
      ) : null}
      <span className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-text-muted" data-preview="">
          {previewPrefix ? <span className="font-medium text-text">{previewPrefix} </span> : null}
          {previewText ? <bdi dir="auto">{previewText}</bdi> : null}
        </span>
        {unread ? <CountBadge value={unread} label={unreadLabel ?? null} /> : null}
      </span>
      {badges ? <span className="flex flex-wrap items-center gap-1">{badges}</span> : null}
      {/* The visible text is the link's name (WCAG 2.5.3); the state words ride along. */}
      {stateLabel ? <span className="sr-only">{stateLabel}</span> : null}
    </>
  );
  const linkProps = {
    href,
    className,
    children: body,
    "data-conversation-row": href,
    ...(selected ? { "aria-current": "page" as const } : {}),
  };
  return (
    <li className="relative flex items-start gap-1" data-row-selected={selected ? "" : undefined}>
      <div className="min-w-0 flex-1">
        {renderLink ? (
          renderLink(linkProps)
        ) : (
          <a
            href={linkProps.href}
            className={linkProps.className}
            aria-current={linkProps["aria-current"]}
            data-conversation-row={linkProps["data-conversation-row"]}
          >
            {body}
          </a>
        )}
      </div>
      {menu ? <div className="pt-2">{menu}</div> : null}
    </li>
  );
}

/* ------------------------------------------------------------------ the list's frame */

export interface ConversationListProps {
  /** The list's accessible name ("Conversations"). */
  label: string;
  children?: ReactNode;
  loading?: boolean;
  loadingLabel?: string;
  /** An empty or error state, drawn in place of the rows. */
  state?: ReactNode;
  /** "Load more" when a next page exists. */
  loadMoreLabel?: string;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  /** "Updated — show latest" while an update waits for the person (IL-06). */
  updateLabel?: string | null;
  onShowUpdate?: () => void;
  /** The node an IntersectionObserver watches to load the next page. */
  sentinelRef?: (node: HTMLElement | null) => void;
}

export function ConversationList({
  label,
  children,
  loading = false,
  loadingLabel,
  state,
  loadMoreLabel,
  onLoadMore,
  loadingMore = false,
  updateLabel,
  onShowUpdate,
  sentinelRef,
}: ConversationListProps) {
  return (
    <div className="flex flex-col gap-2" data-conversation-list="">
      {updateLabel && onShowUpdate ? (
        <Button variant="secondary" onClick={onShowUpdate} data-list-update="">
          {updateLabel}
        </Button>
      ) : null}
      {loading ? (
        <ul aria-busy="true" aria-label={label} className="m-0 flex list-none flex-col gap-2 p-0">
          {Array.from({ length: 6 }, (_, index) => (
            <li
              key={index}
              className="flex flex-col gap-2 rounded-lg bg-surface px-4 py-3"
              aria-hidden="true"
            >
              <span className="h-4 w-1/2 rounded-sm bg-secondary-subtle" />
              <span className="h-3 w-3/4 rounded-sm bg-secondary-subtle" />
            </li>
          ))}
          {loadingLabel ? <li className="sr-only">{loadingLabel}</li> : null}
        </ul>
      ) : state ? (
        state
      ) : (
        <ul aria-label={label} className="m-0 flex list-none flex-col gap-1 p-0">
          {children}
        </ul>
      )}
      {!loading && !state && loadMoreLabel && onLoadMore ? (
        <div ref={sentinelRef} className="flex justify-center py-2">
          <Button variant="ghost" onClick={onLoadMore} busy={loadingMore} data-load-more="">
            {loadMoreLabel}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ bubbles */

export type DeliveryState =
  "sending" | "pending" | "sent" | "delivered" | "read" | "failed" | "received";

const deliveryIcon: Record<DeliveryState, { icon: LucideIcon; name: string } | null> = {
  sending: { icon: Clock, name: "Clock" },
  pending: { icon: Clock, name: "Clock" },
  sent: { icon: Check, name: "Check" },
  delivered: { icon: CheckCheck, name: "CheckCheck" },
  read: { icon: CheckCheck, name: "CheckCheck" },
  failed: { icon: AlertCircle, name: "AlertCircle" },
  received: null,
};

export interface MessageBubbleProps {
  side: "in" | "out";
  /** Above the first bubble of a group: "AI", a member's name, or nothing for the customer. */
  author?: ReactNode;
  /** "Template" and similar marks, as words. */
  marks?: ReactNode;
  /** The message's text, already split by the app into strings and link buttons. */
  children?: ReactNode;
  /** Media tiles, in position order. */
  media?: ReactNode;
  time?: string;
  timeFull?: string;
  delivery?: { state: DeliveryState; label: string } | null;
  /** A failed send: its reason and actions (Retry, Discard). */
  failure?: { reason: string; actions?: ReactNode } | null;
  firstOfGroup?: boolean;
  lastOfGroup?: boolean;
  messageId?: string;
}

export function MessageBubble({
  side,
  author,
  marks,
  children,
  media,
  time,
  timeFull,
  delivery,
  failure,
  firstOfGroup = true,
  lastOfGroup = true,
  messageId,
}: MessageBubbleProps) {
  const mark = delivery ? deliveryIcon[delivery.state] : null;
  return (
    <li
      data-bubble={side}
      data-message-id={messageId}
      className={cn(
        "flex max-w-bubble flex-col gap-1",
        side === "in" ? "items-start self-start" : "items-end self-end",
        firstOfGroup ? "mt-3" : "mt-1",
      )}
    >
      {firstOfGroup && author ? (
        <span className="px-2 text-xs font-medium text-text-muted">{author}</span>
      ) : null}
      <div
        className={cn(
          "flex max-w-full flex-col gap-2 rounded-lg px-4 py-3 text-md text-text",
          side === "in" ? "border border-border bg-surface-raised" : "bg-secondary-subtle",
          failure ? "border border-danger" : null,
        )}
      >
        {marks ? <span className="flex flex-wrap gap-1">{marks}</span> : null}
        {media}
        {children ? (
          <p dir="auto" className="m-0 whitespace-pre-wrap break-words" data-message-text="">
            {children}
          </p>
        ) : null}
      </div>
      {failure ? (
        <div
          role="status"
          className="flex flex-wrap items-center gap-2 rounded-md bg-danger-subtle px-3 py-2 text-sm text-danger"
          data-bubble-failure=""
        >
          <Icon icon={AlertCircle} name="AlertCircle" size={16} />
          <span>{failure.reason}</span>
          {failure.actions}
        </div>
      ) : null}
      {lastOfGroup && (time || delivery) ? (
        <span className="flex items-center gap-1 px-2 text-xs text-text-muted">
          {time ? (
            <time title={timeFull} aria-label={timeFull}>
              {time}
            </time>
          ) : null}
          {delivery && mark ? (
            <span className="inline-flex items-center gap-1" data-delivery={delivery.state}>
              <Icon icon={mark.icon} name={mark.name} size={14} />
              <span>{delivery.label}</span>
            </span>
          ) : null}
        </span>
      ) : null}
    </li>
  );
}

/** A centred, muted line in the thread: a day, or "Seen by …". */
export function ThreadNote({
  children,
  kind = "day",
}: {
  children: ReactNode;
  kind?: "day" | "seen";
}) {
  return (
    <li
      data-thread-note={kind}
      className={cn(
        "flex text-xs text-text-muted",
        kind === "day" ? "my-3 items-center gap-3 self-stretch" : "self-end px-2",
      )}
    >
      {kind === "day" ? <span aria-hidden="true" className="h-px flex-1 bg-border" /> : null}
      <span>{children}</span>
      {kind === "day" ? <span aria-hidden="true" className="h-px flex-1 bg-border" /> : null}
    </li>
  );
}

/** The link inside customer text: looks like a link, is a Button that asks first (TH-02). */
export function InertLink({
  children,
  onActivate,
}: {
  children: ReactNode;
  onActivate: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onActivate}
      data-inert-link=""
      className={cn(
        "inline cursor-pointer break-all rounded-sm bg-transparent p-0 text-start text-primary underline underline-offset-4 hover:decoration-2",
        focusRing,
      )}
    >
      <bdi dir="ltr">{children}</bdi>
    </button>
  );
}

export interface LinkConfirmProps {
  url: string | null;
  title: string;
  description: string;
  openLabel: string;
  cancelLabel: string;
  closeLabel: string;
  onOpen: (url: string) => void;
  onClose: () => void;
}

/** "Open this link?" with the full address shown before anything opens (TH-02). */
export function LinkConfirm({
  url,
  title,
  description,
  openLabel,
  cancelLabel,
  closeLabel,
  onOpen,
  onClose,
}: LinkConfirmProps) {
  return (
    <Dialog
      open={url !== null}
      onOpenChange={(open) => (open ? undefined : onClose())}
      title={title}
      description={description}
      closeLabel={closeLabel}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {cancelLabel}
          </Button>
          <Button
            variant="primary"
            onClick={() => {
              if (url) onOpen(url);
              onClose();
            }}
          >
            {openLabel}
          </Button>
        </>
      }
    >
      <p
        className="m-0 break-all rounded-md bg-surface-sunken px-3 py-2 text-sm text-text"
        data-link-target=""
      >
        <bdi dir="ltr">{url}</bdi>
      </p>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ media */

export type MediaTileState =
  "ready" | "loading" | "ask" | "pending" | "failed" | "unavailable" | "unsupported";

export interface MediaTileProps {
  kind: "image" | "audio" | "video" | "document";
  state: MediaTileState;
  /** A blob: URL for a ready image / audio / video. */
  src?: string | null;
  alt?: string;
  /** Words for the non-ready states and the file row. */
  label: string;
  filename?: string | null;
  size?: string | null;
  /** "Load image", "Play voice message", "Download". */
  actionLabel?: string;
  onAction?: () => void;
  /** Activating a ready image opens the viewer. */
  onOpen?: () => void;
  openLabel?: string;
  caption?: ReactNode;
}

const stateIcon: Record<string, { icon: LucideIcon; name: string }> = {
  pending: { icon: Clock, name: "Clock" },
  failed: { icon: AlertCircle, name: "AlertCircle" },
  unavailable: { icon: ImageOff, name: "ImageOff" },
};

const kindIcon: Record<MediaTileProps["kind"], { icon: LucideIcon; name: string }> = {
  image: { icon: FileText, name: "FileText" },
  audio: { icon: Mic, name: "Mic" },
  video: { icon: Film, name: "Film" },
  document: { icon: FileText, name: "FileText" },
};

export function MediaTile({
  kind,
  state,
  src,
  alt,
  label,
  filename,
  size,
  actionLabel,
  onAction,
  onOpen,
  openLabel,
  caption,
}: MediaTileProps) {
  const frame = "flex w-full max-w-media flex-col gap-2 rounded-md";
  if (state === "pending" || state === "failed" || state === "unavailable") {
    const mark = stateIcon[state]!;
    return (
      <div
        data-media-state={state}
        className={cn(
          frame,
          "border border-border bg-surface-sunken px-3 py-3 text-sm text-text-muted",
        )}
      >
        <span className={cn("flex items-center gap-2", state === "failed" ? "text-danger" : null)}>
          <Icon icon={mark.icon} name={mark.name} size={16} />
          <span>{label}</span>
        </span>
        {caption}
      </div>
    );
  }
  if (kind === "image" && state === "ready" && src) {
    return (
      <div data-media-state="ready" className={frame}>
        <button
          type="button"
          onClick={onOpen}
          aria-label={openLabel}
          className={cn("block overflow-hidden rounded-md border border-border p-0", focusRing)}
        >
          <img src={src} alt={alt ?? ""} className="block max-h-media w-full object-contain" />
        </button>
        {caption}
      </div>
    );
  }
  if ((kind === "audio" || kind === "video") && state === "ready" && src) {
    return (
      <div data-media-state="ready" className={frame}>
        {kind === "audio" ? (
          // Customer media carries no caption file; a voice note's transcript (TranscriptBlock) is
          // its text alternative. The empty track keeps the element's caption slot declared.
          <audio controls src={src} className="w-full" aria-label={label}>
            <track kind="captions" />
          </audio>
        ) : (
          <video controls src={src} className="w-full rounded-md" aria-label={label}>
            <track kind="captions" />
          </video>
        )}
        {caption}
      </div>
    );
  }
  const mark = kindIcon[kind];
  return (
    <div
      data-media-state={state}
      className={cn(frame, "border border-border bg-surface px-3 py-3")}
    >
      <span className="flex items-center gap-2 text-sm text-text">
        <Icon icon={mark.icon} name={mark.name} size={18} />
        <span className="min-w-0 flex-1 truncate" title={filename ?? label}>
          <bdi>{filename ?? label}</bdi>
        </span>
        {size ? (
          <span className="shrink-0 text-xs text-text-muted">
            <bdi>{size}</bdi>
          </span>
        ) : null}
      </span>
      {actionLabel && onAction ? (
        <Button
          variant="secondary"
          onClick={onAction}
          busy={state === "loading"}
          data-media-action=""
        >
          {kind === "document" ? <Icon icon={Download} name="Download" size={16} /> : null}
          {actionLabel}
        </Button>
      ) : null}
      {caption}
    </div>
  );
}

/** A transcript under a voice note, behind a disclosure when it is long (TH-07). */
export function TranscriptBlock({
  label,
  state,
  text,
  stateLabel,
  showLabel,
  hideLabel,
  long = false,
}: {
  label: string;
  state: "ready" | "pending" | "failed" | "none";
  text?: string | null;
  stateLabel?: string;
  showLabel: string;
  hideLabel: string;
  long?: boolean;
}) {
  const [open, setOpen] = useState(!long);
  const id = useId();
  if (state === "none") return null;
  if (state !== "ready" || !text) {
    return (
      <p className="m-0 text-sm text-text-muted" data-transcript={state}>
        {stateLabel}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-1" data-transcript="ready">
      <span className="text-xs font-medium text-text-muted">{label}</span>
      {long ? (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((value) => !value)}
          className={cn(
            "self-start rounded-sm text-sm text-primary underline underline-offset-4",
            focusRing,
          )}
        >
          {open ? hideLabel : showLabel}
        </button>
      ) : null}
      <p id={id} hidden={!open} dir="auto" className="m-0 whitespace-pre-wrap text-sm text-text">
        {text}
      </p>
    </div>
  );
}

/** The image viewer (TH-06): FE-1's Dialog, the image at its size up to the viewport. */
export function MediaViewer({
  open,
  src,
  alt,
  title,
  caption,
  closeLabel,
  downloadLabel,
  onDownload,
  onClose,
  onPrevious,
  onNext,
  previousLabel,
  nextLabel,
}: {
  open: boolean;
  src: string | null;
  alt: string;
  title: string;
  caption?: ReactNode;
  closeLabel: string;
  downloadLabel: string;
  onDownload: () => void;
  onClose: () => void;
  onPrevious?: (() => void) | null;
  onNext?: (() => void) | null;
  previousLabel: string;
  nextLabel: string;
}) {
  // Arrows move between the images already loaded, in the reading direction (TH-06).
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const forward = (event.key === "ArrowRight") !== (document.dir === "rtl");
      (forward ? onNext : onPrevious)?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onNext, onPrevious]);
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={title}
      closeLabel={closeLabel}
      footer={
        <>
          {onPrevious ? (
            <Button variant="ghost" onClick={onPrevious}>
              {previousLabel}
            </Button>
          ) : null}
          {onNext ? (
            <Button variant="ghost" onClick={onNext}>
              {nextLabel}
            </Button>
          ) : null}
          <Button variant="secondary" onClick={onDownload}>
            {downloadLabel}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {src ? (
          <img
            src={src}
            alt={alt}
            className="mx-auto block max-h-inbox max-w-full object-contain"
          />
        ) : null}
        {caption ? <div className="text-sm text-text">{caption}</div> : null}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ the composer */

export interface ComposerProps {
  /** A notice above the field: the first-reply line, the window's time left. */
  notice?: ReactNode;
  /** Replaces the field in the stopped states (window closed, opted out, closed). */
  stopped?: ReactNode;
  /** The attachment chip, when a file is chosen. */
  attachment?: ReactNode;
  field?: ReactNode;
  /** The secondary controls (Attach, Template) at the inline start. */
  tools?: ReactNode;
  /** The one primary (Send, or Choose template when the window is closed). */
  primary?: ReactNode;
  counter?: ReactNode;
  error?: ReactNode;
  /** A file dragged over the pane. */
  dropActive?: boolean;
  dropLabel?: string;
}

export function Composer({
  notice,
  stopped,
  attachment,
  field,
  tools,
  primary,
  counter,
  error,
  dropActive = false,
  dropLabel,
}: ComposerProps) {
  return (
    <div
      data-composer=""
      className={cn(
        "flex flex-col gap-2 border-t border-border bg-surface px-4 py-3",
        dropActive ? "outline-2 outline-dashed outline-primary" : null,
      )}
    >
      {dropActive && dropLabel ? <p className="m-0 text-sm text-primary">{dropLabel}</p> : null}
      {notice ? <div className="text-sm text-text-muted">{notice}</div> : null}
      {stopped ? (
        <div className="flex flex-wrap items-center justify-between gap-3" data-composer-stopped="">
          {stopped}
        </div>
      ) : (
        <>
          {attachment}
          {field}
          {error ? <div className="text-sm text-danger">{error}</div> : null}
          <div className="flex items-center gap-2">
            <div className="flex flex-1 items-center gap-1">{tools}</div>
            {counter ? <span className="text-xs text-text-muted">{counter}</span> : null}
            {primary}
          </div>
        </>
      )}
    </div>
  );
}

/** The chosen file above the field, with Remove. */
export function AttachmentChip({
  name,
  size,
  removeLabel,
  onRemove,
}: {
  name: string;
  size: string;
  removeLabel: string;
  onRemove: () => void;
}) {
  return (
    <div
      className="flex max-w-full items-center gap-2 self-start rounded-md bg-secondary-subtle px-3 py-1 text-sm text-text"
      data-attachment=""
    >
      <Icon icon={FileText} name="FileText" size={16} />
      <span className="min-w-0 truncate">
        <bdi>{name}</bdi>
      </span>
      <span className="shrink-0 text-xs text-text-muted">
        <bdi>{size}</bdi>
      </span>
      <IconButton icon={X} iconName="X" label={removeLabel} onClick={onRemove} />
    </div>
  );
}

/* ------------------------------------------------------------------ panes */

/** "New messages" at the bottom of a thread scrolled up (TH-01): placed, never moving the thread. */
export function NewMessagesPill({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <div className="pointer-events-none sticky bottom-3 flex justify-center">
      <Button
        variant="secondary"
        onClick={onClick}
        className="pointer-events-auto shadow-md"
        data-new-messages=""
      >
        <Icon icon={ArrowDown} name="ArrowDown" size={16} />
        {label}
      </Button>
    </div>
  );
}

export interface SidePanelProps {
  title: string;
  /** "pane" from 1024 px; "drawer" below it (LY-01). */
  variant: "pane" | "drawer";
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  closeLabel: string;
  children: ReactNode;
  headingId?: string;
}

/** The customer panel's frame: a pane at the inline end, or a drawer from the inline end. */
export const SidePanel = forwardRef<HTMLHeadingElement, SidePanelProps>(function SidePanel(
  { title, variant, open = false, onOpenChange, closeLabel, children, headingId },
  ref,
) {
  const heading = (
    <h2
      ref={ref}
      id={headingId}
      tabIndex={-1}
      className="m-0 text-lg font-semibold text-text outline-none"
    >
      {title}
    </h2>
  );
  if (variant === "pane") {
    return (
      <aside
        aria-labelledby={headingId}
        data-side-panel="pane"
        className="flex w-inbox-panel shrink-0 flex-col overflow-y-auto border-s border-border bg-surface"
      >
        <div className="bg-secondary-subtle px-5 py-4">{heading}</div>
        <div className="flex flex-col gap-5 p-5">{children}</div>
      </aside>
    );
  }
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="wasla-scrim fixed inset-0 z-(--z-overlay) bg-scrim" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          data-side-panel="drawer"
          className="wasla-drawer fixed inset-y-0 end-0 z-(--z-modal) flex w-full max-w-inbox-panel flex-col overflow-y-auto border-s border-border bg-surface shadow-lg"
        >
          <div className="flex items-center justify-between gap-3 bg-secondary-subtle px-5 py-4">
            <DialogPrimitive.Title asChild>{heading}</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <IconButton icon={X} iconName="X" label={closeLabel} />
            </DialogPrimitive.Close>
          </div>
          <div className="flex flex-col gap-5 p-5">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
});
