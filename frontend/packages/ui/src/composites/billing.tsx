"use client";

/**
 * FE-8's composites (UI8-02, ADR-202): the billing screens' presentational parts. Presentational
 * only - every word arrives as a prop, every action as a node the screen builds. Role tokens only;
 * money is passed already formatted (the screen formats the API's own string); untrusted text in
 * <bdi>; no clock, no fetch, no storage.
 */
import { Check, Circle, CircleAlert, TriangleAlert, type LucideIcon } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { Spinner } from "../primitives/button";

/* ------------------------------------------------------- payment status */

export type PaymentStatusTone = "progress" | "success" | "danger" | "warning" | "neutral";

const MARK: Record<Exclude<PaymentStatusTone, "progress">, { icon: LucideIcon; name: string }> = {
  success: { icon: Check, name: "Check" },
  danger: { icon: CircleAlert, name: "CircleAlert" },
  warning: { icon: TriangleAlert, name: "TriangleAlert" },
  neutral: { icon: Circle, name: "Circle" },
};

const BAND: Record<PaymentStatusTone, string> = {
  progress: "bg-info-subtle text-info",
  success: "bg-success-subtle text-success",
  danger: "bg-danger-subtle text-danger",
  warning: "bg-warning-subtle text-warning",
  neutral: "bg-secondary-subtle text-text",
};

/**
 * The return page's one state (RT-05): a calm progress mark while confirming (the spinner stops
 * under reduced motion, as every Wasla spinner does), then the outcome in words with an icon -
 * never colour alone.
 */
export function PaymentStatusPanel({
  tone,
  title,
  description,
  action,
}: {
  tone: PaymentStatusTone;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div
      className="flex flex-col items-center gap-3 px-2 py-6 text-center"
      data-payment-panel={tone}
    >
      <span
        aria-hidden="true"
        className={cn("inline-flex size-14 items-center justify-center rounded-full", BAND[tone])}
      >
        {tone === "progress" ? (
          <Spinner className="size-7" />
        ) : (
          <Icon icon={MARK[tone].icon} name={MARK[tone].name} size={28} />
        )}
      </span>
      <p className="m-0 text-xl font-semibold text-text" data-payment-title="">
        {title}
      </p>
      {description ? (
        <p className="m-0 max-w-prose text-md text-text-muted">{description}</p>
      ) : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

/* --------------------------------------------------------------- usage */

export interface UsageMeterProps {
  label: string;
  /** "12 of 1,000", or "12 used" when unlimited - in the screen's digits. */
  valueText: string;
  used: number;
  /** null: unlimited - no bar is drawn, only the words. */
  limit: number | null;
  /** An extra line ("3 held for replies in progress"). */
  note?: string | null;
  over?: boolean;
}

/** One entitlement against its limit (US-02): the bar is a meter, the words are what it says. */
export function UsageMeter({ label, valueText, used, limit, note, over = false }: UsageMeterProps) {
  const id = useId();
  const ratio = limit && limit > 0 ? Math.max(0, Math.min(1, used / limit)) : 0;
  return (
    <div className="flex flex-col gap-2" data-usage-meter="">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span id={id} className="text-sm font-medium text-text">
          {label}
        </span>
        <span className="text-sm text-text-muted tabular-nums">
          <bdi>{valueText}</bdi>
        </span>
      </div>
      {limit !== null ? (
        <div
          role="meter"
          aria-labelledby={id}
          aria-valuemin={0}
          aria-valuemax={Math.max(limit, 1)}
          aria-valuenow={Math.min(used, Math.max(limit, 1))}
          aria-valuetext={valueText}
          className="h-2 w-full overflow-hidden rounded-full bg-secondary-subtle"
        >
          <div
            className={cn("h-full rounded-full", over ? "bg-danger" : "bg-primary")}
            style={{ width: `${ratio * 100}%` }}
          />
        </div>
      ) : null}
      {note ? <span className="text-xs text-text-muted">{note}</span> : null}
    </div>
  );
}

/* ------------------------------------------------------------ series */

export interface SeriesPoint {
  /** The day as the screen shows it ("20 Nov"). */
  label: string;
  value: number;
  /** The value in the screen's digits. */
  valueText: string;
}

/**
 * A small bar chart drawn with SVG and its table, the table being the accessible source (US-02):
 * the chart is hidden from assistive technology and a toggle shows the same numbers as a table.
 */
export function BarSeries({
  title,
  points,
  dayLabel,
  valueLabel,
  showTable,
  showChart,
  emptyText,
}: {
  title: string;
  points: SeriesPoint[];
  dayLabel: string;
  valueLabel: string;
  showTable: string;
  showChart: string;
  emptyText: string;
}) {
  const [table, setTable] = useState(false);
  const max = Math.max(1, ...points.map((point) => point.value));
  const width = 600;
  const height = 120;
  const step = points.length ? width / points.length : width;
  return (
    <figure className="m-0 flex flex-col gap-3" data-bar-series="">
      <figcaption className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-md font-semibold text-text">{title}</span>
        {points.length ? (
          <button
            type="button"
            onClick={() => setTable(!table)}
            className="min-h-target rounded-md px-2 text-sm font-medium text-primary underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-2"
            data-action="series-view"
          >
            {table ? showChart : showTable}
          </button>
        ) : null}
      </figcaption>
      {!points.length ? (
        <p className="m-0 text-md text-text-muted">{emptyText}</p>
      ) : table ? (
        <div className="max-h-80 overflow-auto rounded-md border border-border">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr>
                <th scope="col" className="px-3 py-2 text-start font-medium text-text-muted">
                  {dayLabel}
                </th>
                <th scope="col" className="px-3 py-2 text-end font-medium text-text-muted">
                  {valueLabel}
                </th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.label} className="border-t border-border">
                  <td className="px-3 py-2 text-text">{point.label}</td>
                  <td className="px-3 py-2 text-end text-text tabular-nums">{point.valueText}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="h-32 w-full"
          aria-hidden="true"
          preserveAspectRatio="none"
          data-series-chart=""
        >
          {points.map((point, index) => {
            const h = (point.value / max) * (height - 4);
            return (
              <rect
                key={point.label}
                x={index * step + step * 0.15}
                y={height - h}
                width={step * 0.7}
                height={h}
                rx={2}
                className="fill-primary"
              />
            );
          })}
        </svg>
      )}
    </figure>
  );
}

/* ------------------------------------------------------------- invoice */

export interface InvoiceLineItem {
  key: string;
  description: string;
  quantity: string;
  amount: string;
}

/** An invoice's lines as a table (IN-02): three columns that wrap on a phone, never a scroll. */
export function InvoiceLines({
  label,
  lines,
  descriptionLabel,
  quantityLabel,
  amountLabel,
}: {
  label: string;
  lines: InvoiceLineItem[];
  descriptionLabel: string;
  quantityLabel: string;
  amountLabel: string;
}) {
  return (
    <div className="rounded-md border border-border bg-surface" data-invoice-lines="">
      <table className="w-full table-fixed border-collapse text-sm">
        <caption className="px-3 py-2 text-start text-md font-semibold text-text">{label}</caption>
        <thead>
          <tr className="border-t border-border">
            <th scope="col" className="w-1/2 px-3 py-2 text-start font-medium text-text-muted">
              {descriptionLabel}
            </th>
            <th scope="col" className="px-3 py-2 text-end font-medium text-text-muted">
              {quantityLabel}
            </th>
            <th scope="col" className="px-3 py-2 text-end font-medium text-text-muted">
              {amountLabel}
            </th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.key} className="border-t border-border">
              <td className="break-words px-3 py-2 text-text">
                <bdi>{line.description}</bdi>
              </td>
              <td className="px-3 py-2 text-end text-text tabular-nums">{line.quantity}</td>
              <td className="px-3 py-2 text-end text-text tabular-nums">{line.amount}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
