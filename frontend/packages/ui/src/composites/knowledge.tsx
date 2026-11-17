"use client";

/**
 * The AI agent and knowledge composites (FE-6, UI6-02, ADR-200): presentational only - no data
 * fetching, no session, no router. Agent names, instructions, knowledge-base names, document
 * titles, errors, passages and customers' questions are text a person, a document or the AI wrote:
 * rendered as React text inside `<bdi>` / `dir="auto"`, never as markup, never Markdown. Model,
 * file and tool names are code text, left to right. A status is always a word; "Answers customers"
 * is not a status, so it never takes a status colour.
 */
import { CircleCheck, FileUp, MessageSquareText } from "lucide-react";
import { useId, useRef, type ChangeEvent, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { Button } from "../primitives/button";
import { Checkbox } from "../primitives/checkbox";
import { RadioGroup } from "../primitives/radio-group";
import { focusRing } from "../primitives/styles";
import { Switch } from "../primitives/switch";
import { rowClass, RowLink, type RenderLink } from "./customers";
import { Badge, type StatusTone } from "./status";

/* ------------------------------------------------------------------ agents */

/** "Answers customers": an accent icon and words - never a status colour (UI6-01). */
export function AnswersMark({ label }: { label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 text-sm font-medium text-primary"
      data-answers=""
    >
      <Icon icon={MessageSquareText} name="MessageSquareText" size={16} />
      {label}
    </span>
  );
}

export interface AgentRowProps {
  href: string;
  /** The agent's name (member text). */
  name: string;
  /** The status as a badge with its word. */
  status: ReactNode;
  /** Present on the one agent that answers customers. */
  answers?: ReactNode;
  /** The model's name (code text). */
  model: string;
  modelLabel: string;
  time?: string | null;
  timeFull?: string | null;
  renderLink?: RenderLink;
}

/** One agent in a list: its name, its status, whether it answers, its model. */
export function AgentRow({
  href,
  name,
  status,
  answers,
  model,
  modelLabel,
  time,
  timeFull,
  renderLink,
}: AgentRowProps) {
  const descriptionId = useId();
  return (
    <li data-agent-row="">
      <RowLink
        href={href}
        className={rowClass}
        renderLink={renderLink}
        marker={{ "data-row-link": href }}
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-md font-semibold">
            <bdi>{name}</bdi>
          </span>
          {answers}
          {status}
        </span>
        <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-muted">
          <span>
            {modelLabel}{" "}
            <bdi dir="ltr" className="font-medium text-text">
              {model}
            </bdi>
          </span>
          {time ? (
            <>
              <time aria-describedby={descriptionId}>{time}</time>
              <span id={descriptionId} className="sr-only">
                {timeFull}
              </span>
            </>
          ) : null}
        </span>
      </RowLink>
    </li>
  );
}

/* ------------------------------------------------------------------ tools */

export interface ToolSwitchRowProps {
  /** The tool's name in words, or its API name when the catalogue does not know it. */
  label: string;
  /** The API name, shown as code text when `label` is that name. */
  apiName?: string | null;
  description: string;
  on: boolean;
  /** No switch: the state in words (a member, or a tool this build no longer runs). */
  readOnly?: boolean;
  stateLabel: string;
  busy?: boolean;
  error?: ReactNode;
  onChange?: (on: boolean) => void;
  /** What sits under the row when it is on (search_knowledge's scope). */
  children?: ReactNode;
  /** An action instead of a switch ("Remove" for a grant no longer available). */
  action?: ReactNode;
}

/** One tool of an agent: what it does and a switch - nothing optimistic, the answer decides. */
export function ToolSwitchRow({
  label,
  apiName,
  description,
  on,
  readOnly = false,
  stateLabel,
  busy = false,
  error,
  onChange,
  children,
  action,
}: ToolSwitchRowProps) {
  const labelId = useId();
  const descriptionId = useId();
  return (
    <li className="flex flex-col gap-3 rounded-lg bg-surface p-4" data-tool-row={apiName ?? label}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <span id={labelId} className="text-md font-semibold text-text">
            {apiName ? (
              <bdi dir="ltr" className="font-mono text-sm">
                {label}
              </bdi>
            ) : (
              label
            )}
          </span>
          <span id={descriptionId} className="text-sm text-text-muted">
            <bdi dir="auto">{description}</bdi>
          </span>
        </div>
        {action ? (
          action
        ) : readOnly ? (
          <span className="shrink-0 text-sm font-medium text-text" data-tool-state="">
            {stateLabel}
          </span>
        ) : (
          <Switch
            checked={on}
            disabled={busy}
            aria-busy={busy || undefined}
            aria-labelledby={labelId}
            aria-describedby={descriptionId}
            onCheckedChange={(next) => onChange?.(next)}
          />
        )}
      </div>
      {error ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {error}
        </p>
      ) : null}
      {children}
    </li>
  );
}

export interface ScopeBase {
  id: string;
  name: string;
}

export interface KnowledgeScopePickerProps {
  legend: string;
  allLabel: string;
  chosenLabel: string;
  choice: "all" | "chosen";
  bases: ScopeBase[];
  chosen: string[];
  /** "Choose at least one knowledge base" when "Only these" has none. */
  problem?: ReactNode;
  disabled?: boolean;
  onChoiceChange: (choice: "all" | "chosen") => void;
  onChosenChange: (chosen: string[]) => void;
  footer?: ReactNode;
}

/** search_knowledge's scope: every knowledge base, or the ones chosen (TL-03). */
export function KnowledgeScopePicker({
  legend,
  allLabel,
  chosenLabel,
  choice,
  bases,
  chosen,
  problem,
  disabled = false,
  onChoiceChange,
  onChosenChange,
  footer,
}: KnowledgeScopePickerProps) {
  const legendId = useId();
  const problemId = useId();
  return (
    <fieldset
      className="m-0 flex flex-col gap-3 rounded-lg border-0 bg-secondary-subtle p-4"
      aria-describedby={problem ? problemId : undefined}
      data-scope-picker=""
    >
      <legend id={legendId} className="mb-2 p-0 text-sm font-medium text-text">
        {legend}
      </legend>
      <RadioGroup
        aria-labelledby={legendId}
        value={choice}
        disabled={disabled}
        onValueChange={(value) => onChoiceChange(value as "all" | "chosen")}
        options={[
          { value: "all", label: allLabel },
          { value: "chosen", label: chosenLabel },
        ]}
      />
      {choice === "chosen" ? (
        <ul className="m-0 flex list-none flex-col gap-2 ps-8" data-scope-bases="">
          {bases.map((base) => {
            const id = `${legendId}-${base.id}`;
            const checked = chosen.includes(base.id);
            return (
              <li key={base.id} className="flex items-center gap-2">
                <Checkbox
                  id={id}
                  checked={checked}
                  disabled={disabled}
                  onCheckedChange={(next) =>
                    onChosenChange(
                      next === true
                        ? [...chosen, base.id]
                        : chosen.filter((value) => value !== base.id),
                    )
                  }
                />
                <label htmlFor={id} className="text-sm text-text">
                  <bdi>{base.name}</bdi>
                </label>
              </li>
            );
          })}
        </ul>
      ) : null}
      {problem ? (
        <p id={problemId} role="alert" className="m-0 text-sm font-medium text-danger">
          {problem}
        </p>
      ) : null}
      {footer}
    </fieldset>
  );
}

/* ------------------------------------------------------------------ documents */

const DOCUMENT_TONE: Record<string, StatusTone> = {
  ready: "success",
  failed: "danger",
  pending: "neutral",
  processing: "neutral",
};

/** A document's status as a word and, for the two outcomes, a status colour. */
export function DocumentStatusBadge({ status, label }: { status: string; label: string }) {
  const tone = DOCUMENT_TONE[status] ?? "neutral";
  return (
    <span data-document-status={status} className="inline-flex">
      <Badge tone={tone} icon={tone !== "neutral"}>
        {label}
      </Badge>
    </span>
  );
}

export interface DocumentRowProps {
  /** The row opens the document's panel. */
  onOpen: () => void;
  title: string;
  status: ReactNode;
  /** "Needs re-indexing", "Re-indexing…": quiet words beside the status. */
  marks?: string[];
  type: string;
  size: string;
  time?: string | null;
  timeFull?: string | null;
  selected?: boolean;
}

/** One document in a list: its title, status, type, size and time; a button that opens it. */
export function DocumentRow({
  onOpen,
  title,
  status,
  marks,
  type,
  size,
  time,
  timeFull,
  selected = false,
}: DocumentRowProps) {
  const descriptionId = useId();
  return (
    <li data-document-row="">
      <button
        type="button"
        onClick={onOpen}
        // Its name is everything it shows (title, status, type, size, time): what is read is what is seen.
        aria-current={selected || undefined}
        className={cn(
          rowClass,
          "cursor-pointer border-0 bg-transparent",
          selected && "bg-secondary-subtle",
        )}
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-md font-semibold">
            <bdi dir="auto">{title}</bdi>
          </span>
          {status}
        </span>
        <span className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-text-muted">
          <span>{type}</span>
          <span>
            <bdi>{size}</bdi>
          </span>
          {time ? (
            <>
              <time aria-describedby={descriptionId}>{time}</time>
              <span id={descriptionId} className="sr-only">
                {timeFull}
              </span>
            </>
          ) : null}
          {marks?.map((mark) => (
            <span key={mark} className="font-medium text-text" data-document-mark="">
              {mark}
            </span>
          ))}
        </span>
      </button>
    </li>
  );
}

/** "1,234 / 20,000": announced only near the bound, so typing does not chatter. */
export function TextCounter({
  id,
  length,
  max,
  text,
}: {
  id?: string;
  length: number;
  max: number;
  /** The counter in the page's digits, e.g. "1,234 / 20,000". */
  text: string;
}) {
  const near = length >= max * 0.9;
  return (
    <p
      id={id}
      className={cn("m-0 text-sm", length > max ? "font-medium text-danger" : "text-text-muted")}
      aria-live={near ? "polite" : "off"}
      data-text-counter=""
    >
      {text}
    </p>
  );
}

export interface FileFieldProps {
  label: string;
  chooseLabel: string;
  /** The chosen file's name and size, already words. */
  chosen?: string | null;
  accept: string;
  hint?: ReactNode;
  /** A pre-check's refusal: announced, and nothing is sent. */
  problem?: ReactNode;
  disabled?: boolean;
  onFile: (file: File | null) => void;
}

/**
 * A labelled file input (KD-03): the platform's own chooser, opened by a button-sized control from
 * the keyboard. No drag-and-drop library; nothing is read here but the name and the size.
 */
export function FileField({
  label,
  chooseLabel,
  chosen,
  accept,
  hint,
  problem,
  disabled = false,
  onFile,
}: FileFieldProps) {
  const labelId = useId();
  const hintId = useId();
  const problemId = useId();
  const input = useRef<HTMLInputElement>(null);
  const described = [hint ? hintId : null, problem ? problemId : null].filter(Boolean).join(" ");
  return (
    <div role="group" aria-labelledby={labelId} className="flex flex-col gap-2" data-file-field="">
      <span id={labelId} className="text-sm font-medium text-text">
        {label}
      </span>
      <div className="flex flex-wrap items-center gap-3">
        {/* The platform's chooser, opened by the button: the button is the one keyboard stop, so
            its focus ring is the one a person sees. */}
        <input
          ref={input}
          type="file"
          accept={accept}
          disabled={disabled}
          tabIndex={-1}
          aria-hidden
          className="sr-only"
          data-file-input=""
          onChange={(event: ChangeEvent<HTMLInputElement>) => {
            onFile(event.target.files?.[0] ?? null);
            event.target.value = "";
          }}
        />
        <Button
          variant="secondary"
          disabled={disabled}
          aria-describedby={described || undefined}
          aria-invalid={problem ? true : undefined}
          onClick={() => input.current?.click()}
        >
          <Icon icon={FileUp} name="FileUp" size={16} />
          {chooseLabel}
        </Button>
        {chosen ? (
          <span className="text-sm text-text" data-file-chosen="">
            <bdi dir="auto">{chosen}</bdi>
          </span>
        ) : null}
      </div>
      {hint ? (
        <p id={hintId} className="m-0 text-sm text-text-muted">
          {hint}
        </p>
      ) : null}
      {problem ? (
        <p id={problemId} role="alert" className="m-0 text-sm font-medium text-danger">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ search and gaps */

export interface PassageItem {
  key: string;
  /** The document's title (text), and what opens it. */
  title: string;
  onOpen?: () => void;
  /** The passage (document text): React text, line breaks kept. */
  text: string;
  /** "Strong match", "Possible match": words. */
  match: string;
}

/** What a test question would be answered from: each passage as text, never markup. */
export function PassageList({ label, passages }: { label: string; passages: PassageItem[] }) {
  return (
    <ol aria-label={label} className="m-0 flex list-none flex-col gap-3 p-0" data-passage-list="">
      {passages.map((passage) => (
        <li key={passage.key} className="flex flex-col gap-2 rounded-lg bg-surface p-4">
          <span className="flex flex-wrap items-center gap-2">
            {passage.onOpen ? (
              <button
                type="button"
                onClick={passage.onOpen}
                className={cn(
                  "min-h-target cursor-pointer border-0 bg-transparent p-0 text-start text-md font-semibold text-primary underline underline-offset-4",
                  focusRing,
                )}
              >
                <bdi dir="auto">{passage.title}</bdi>
              </button>
            ) : (
              <span className="text-md font-semibold text-text">
                <bdi dir="auto">{passage.title}</bdi>
              </span>
            )}
            <span className="text-sm text-text-muted">{passage.match}</span>
          </span>
          <p dir="auto" className="m-0 whitespace-pre-line text-sm text-text">
            {passage.text}
          </p>
        </li>
      ))}
    </ol>
  );
}

export interface GapItem {
  key: string;
  /** What a customer's question became (the model's query): text, never a link. */
  query: string;
  searches: string;
  lastAsked: string;
  lastAskedFull: string;
}

/** The questions nothing answered, most asked first, as the API sends them. */
export function GapList({
  label,
  searchesLabel,
  lastAskedLabel,
  gaps,
}: {
  label: string;
  searchesLabel: string;
  lastAskedLabel: string;
  gaps: GapItem[];
}) {
  return (
    <ol aria-label={label} className="m-0 flex list-none flex-col gap-2 p-0" data-gap-list="">
      {gaps.map((gap) => (
        <li key={gap.key} className="flex flex-col gap-1 rounded-lg bg-surface px-4 py-3">
          <span className="text-md text-text">
            <bdi dir="auto">{gap.query}</bdi>
          </span>
          <span className="flex flex-wrap gap-x-4 text-sm text-text-muted">
            <span>
              {searchesLabel} <bdi className="font-medium text-text">{gap.searches}</bdi>
            </span>
            <span>
              {lastAskedLabel} <time title={gap.lastAskedFull}>{gap.lastAsked}</time>
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}

export interface SettingSwitchRowProps {
  label: string;
  description: string;
  /** "Uses the default" or "Set for this workspace". */
  sourceLabel: string;
  overridden: boolean;
  /** The control: a Switch or a RadioGroup, already labelled by `labelId`. */
  control: (ids: { labelId: string; descriptionId: string }) => ReactNode;
  useDefaultLabel: string;
  onUseDefault: () => void;
  busy?: boolean;
}

/** One answer setting: its effective value, where it comes from, and "Use the default". */
export function SettingSwitchRow({
  label,
  description,
  sourceLabel,
  overridden,
  control,
  useDefaultLabel,
  onUseDefault,
  busy = false,
}: SettingSwitchRowProps) {
  const labelId = useId();
  const descriptionId = useId();
  return (
    <li className="flex flex-col gap-2 rounded-lg bg-surface p-4" data-setting-row="">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <span id={labelId} className="text-md font-semibold text-text">
            {label}
          </span>
          <span id={descriptionId} className="text-sm text-text-muted">
            {description}
          </span>
        </div>
        {control({ labelId, descriptionId })}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-sm text-text-muted">
        <span
          className="inline-flex items-center gap-1"
          data-setting-source={overridden ? "workspace" : "default"}
        >
          {overridden ? null : <Icon icon={CircleCheck} name="CircleCheck" size={14} />}
          {sourceLabel}
        </span>
        {overridden ? (
          <Button variant="ghost" disabled={busy} onClick={onUseDefault}>
            {useDefaultLabel}
          </Button>
        ) : null}
      </div>
    </li>
  );
}
