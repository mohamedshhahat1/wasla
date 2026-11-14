/**
 * Time in the inbox (TH-02, IL-03): groups of messages, day separators in the viewer's own time
 * zone, relative times - all with an injected clock, so no test depends on the wall clock, the
 * date or the machine's time zone.
 */

export const GROUP_GAP_MS = 5 * 60 * 1000;

export interface Timed {
  id: string;
  side: "in" | "out";
  createdAt: string;
}

export type ThreadItem<T> =
  | { kind: "day"; key: string; day: string }
  | { kind: "message"; key: string; message: T; firstOfGroup: boolean; lastOfGroup: boolean };

/** The calendar day of an instant in a time zone, as YYYY-MM-DD. */
export function dayOf(iso: string, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Messages oldest first → day separators and groups (same side within five minutes). */
export function threadItems<T extends Timed>(
  messages: readonly T[],
  timeZone?: string,
): ThreadItem<T>[] {
  const items: ThreadItem<T>[] = [];
  let lastDay = "";
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]!;
    const day = dayOf(message.createdAt, timeZone);
    if (day !== lastDay) {
      items.push({ kind: "day", key: `day-${day}`, day });
      lastDay = day;
    }
    const previous = messages[index - 1];
    const next = messages[index + 1];
    const joins = (a: T | undefined, b: T | undefined) =>
      Boolean(
        a &&
        b &&
        a.side === b.side &&
        dayOf(a.createdAt, timeZone) === dayOf(b.createdAt, timeZone) &&
        Math.abs(Date.parse(b.createdAt) - Date.parse(a.createdAt)) <= GROUP_GAP_MS,
      );
    items.push({
      kind: "message",
      key: message.id,
      message,
      firstOfGroup: !joins(previous, message),
      lastOfGroup: !joins(message, next),
    });
  }
  return items;
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "5 minutes ago" in the UI's language with Latin digits; "now" under a minute. */
export function relativeTime(iso: string, now: number, locale: string): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  const format = new Intl.RelativeTimeFormat(`${locale}-u-nu-latn`, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return format.format(0, "second");
}

/** The full date and time in the viewer's time zone (an element's accessible description). */
export function fullTime(iso: string, locale: string, timeZone?: string): string {
  return new Intl.DateTimeFormat(`${locale}-u-nu-latn`, {
    dateStyle: "full",
    timeStyle: "short",
    timeZone,
  }).format(new Date(iso));
}

export function clockTime(iso: string, locale: string, timeZone?: string): string {
  return new Intl.DateTimeFormat(`${locale}-u-nu-latn`, { timeStyle: "short", timeZone }).format(
    new Date(iso),
  );
}

export function dayLabel(day: string, locale: string): string {
  const [year, month, date] = day.split("-").map(Number) as [number, number, number];
  return new Intl.DateTimeFormat(`${locale}-u-nu-latn`, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(Date.UTC(year, month - 1, date));
}

/** "1 h 20 m" style duration for the window warning. */
export function durationParts(ms: number): { hours: number; minutes: number } {
  const minutes = Math.max(0, Math.floor(ms / 60000));
  return { hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}
