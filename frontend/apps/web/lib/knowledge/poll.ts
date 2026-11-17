/**
 * Following a document's status by polling (PO-01 .. PO-03, FD-01). There is no stream event for
 * it (FEB-11 is deferred, scale-triggered: past ~50 documents pending at once in one tab, or more
 * than one poll per second per workspace). One loop per list, never per row: 3 s, then ×1.5 after
 * each poll that changed nothing, capped at 15 s; a change resets it; it stops when every loaded
 * row is ready (with no re-index in flight) or failed. Pure, so the sequence is unit-tested.
 */

export const POLL_FIRST_MS = 3_000;
export const POLL_MAX_MS = 15_000;
export const POLL_FACTOR = 1.5;

export interface DocumentLike {
  id: string;
  status: string;
  indexing?: { state: string } | null;
}

const IN_FLIGHT = new Set(["pending", "processing"]);

/** A re-index in flight on a ready document (its latest attempt is pending or processing). */
export function reindexing(document: DocumentLike): boolean {
  return document.status === "ready" && IN_FLIGHT.has(document.indexing?.state ?? "");
}

/** Terminal: ready with nothing in flight, or failed - the plan's "stops at ready / failed". */
export function isTerminal(document: DocumentLike): boolean {
  if (document.status === "failed") return true;
  return document.status === "ready" && !reindexing(document);
}

export function needsPolling(documents: readonly DocumentLike[]): boolean {
  return documents.some((document) => !isTerminal(document));
}

/** The interval after a poll: back to the first after a change, else ×1.5 up to the cap. */
export function nextInterval(previous: number | null, changed: boolean): number {
  if (previous === null || changed) return POLL_FIRST_MS;
  return Math.min(POLL_MAX_MS, Math.round(previous * POLL_FACTOR));
}

/** A fingerprint of what polling watches, to tell "changed" from "the same". */
export function statusFingerprint(documents: readonly DocumentLike[]): string {
  return documents
    .map((document) => `${document.id}:${document.status}:${document.indexing?.state ?? ""}`)
    .join("|");
}

/** The ids that became terminal between two polls (for the one polite announcement per poll). */
export function newlySettled(
  before: readonly DocumentLike[],
  after: readonly DocumentLike[],
): { ready: number; failed: number } {
  const previous = new Map(before.map((document) => [document.id, document]));
  let ready = 0;
  let failed = 0;
  for (const document of after) {
    const was = previous.get(document.id);
    if (!was || isTerminal(was) || !isTerminal(document)) continue;
    if (document.status === "failed") failed += 1;
    else ready += 1;
  }
  return { ready, failed };
}
