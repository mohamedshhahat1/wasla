/**
 * Customer text in the inbox (TH-02). Text only: these helpers split it into strings and link
 * candidates; the screen draws both as React text, a link as a Button behind a confirmation
 * (LinkConfirm) - never a navigable `<a href>` made from customer text, never fetched, never a
 * preview. Only http and https are offered; anything else stays plain text.
 */

export type TextPart = { kind: "text"; text: string } | { kind: "link"; text: string; url: string };

// A URL candidate: http(s)://… up to whitespace. Trailing punctuation that usually ends a sentence
// is given back to the text.
const CANDIDATE = /https?:\/\/[^\s<>"]+/gi;
const TRAILING = /[.,;:!?)\]}'»”]+$/;

export function safeHttpUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

export function splitLinks(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let last = 0;
  for (const match of text.matchAll(CANDIDATE)) {
    let candidate = match[0];
    const trailing = candidate.match(TRAILING)?.[0] ?? "";
    candidate = candidate.slice(0, candidate.length - trailing.length);
    const start = match.index ?? 0;
    const url = safeHttpUrl(candidate);
    if (!url) continue;
    if (start > last) parts.push({ kind: "text", text: text.slice(last, start) });
    parts.push({ kind: "link", text: candidate, url });
    last = start + candidate.length;
  }
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts.length ? parts : [{ kind: "text", text }];
}

/** Opens a confirmed link in a new page without referrer or opener. */
export function openConfirmed(
  url: string,
  open: (url: string, target: string, features: string) => unknown = (...args) =>
    window.open(...args),
): void {
  const safe = safeHttpUrl(url);
  if (!safe) return;
  open(safe, "_blank", "noopener,noreferrer");
}
