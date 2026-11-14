/**
 * Media in the inbox (TH-03, TH-04, CP-03). Bytes are fetched with the session's own client (its
 * bearer, its gate middleware) as a Blob and shown through an object URL - an `<img src>` cannot
 * carry a bearer, and an API URL never reaches the DOM. Every object URL lives in one per-tab
 * registry and is revoked when its owner lets go, when the conversation changes, on sign-out, on
 * a workspace switch and on page hide. Blobs live in memory only, at most `CACHE_LIMIT_BYTES` per
 * tab, oldest dropped first; never Cache Storage, IndexedDB or a service worker.
 */

/** The canonical send types (app/core/media_types.py CANONICAL_TYPES; plan section 11). */
export const SEND_TYPES: readonly string[] = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "audio/ogg",
  "audio/mpeg",
  "audio/mp4",
  "audio/amr",
  "audio/aac",
  "audio/wav",
  "audio/webm",
  "video/mp4",
  "video/3gpp",
  "video/webm",
  "application/pdf",
  "text/plain",
  "text/csv",
  "application/msword",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
];

/** MEDIA_MAX_BYTES (25 MiB). */
export const SEND_MAX_BYTES = 25 * 1024 * 1024;
/** An image at most this large is fetched when its tile comes into view; larger asks first. */
export const AUTO_IMAGE_BYTES = 2 * 1024 * 1024;
export const MAX_IN_FLIGHT = 3;
export const CACHE_LIMIT_BYTES = 50 * 1024 * 1024;

export const IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"] as const;

export type SendCheck = { ok: true } | { ok: false; reason: "type" | "size" | "empty" };

/** The composer's check before any request (CP-03). */
export function checkFileToSend(file: { type: string; size: number }): SendCheck {
  if (file.size === 0) return { ok: false, reason: "empty" };
  if (file.size > SEND_MAX_BYTES) return { ok: false, reason: "size" };
  const type = file.type.split(";", 1)[0]!.trim().toLocaleLowerCase("en-US");
  if (!SEND_TYPES.includes(type)) return { ok: false, reason: "type" };
  return { ok: true };
}

/** Whether a fetched blob may be drawn by a tile of this kind (TH-04's type check). */
export function blobFits(
  kind: "image" | "audio" | "video" | "document",
  blobType: string,
): boolean {
  const type = blobType.split(";", 1)[0]!.trim().toLocaleLowerCase("en-US");
  if (kind === "image") return (IMAGE_TYPES as readonly string[]).includes(type);
  if (kind === "audio") return type.startsWith("audio/");
  if (kind === "video") return type.startsWith("video/");
  return true;
}

/** A file name safe to offer as a download's name (no path, no control characters). */
export function safeFilename(name: string | null | undefined, fallback: string): string {
  const base = (name ?? "")
    .split(/[\\/]/)
    .pop()!
    .split("")
    .filter((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
    .join("")
    .replace(/[<>:"|?*]/g, "")
    .trim()
    .slice(0, 200);
  return base || fallback;
}

export interface UrlApi {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
}

interface Entry {
  url: string;
  blob: Blob;
  owners: Set<string>;
  usedAt: number;
}

/**
 * Object URLs by media id, with their owners. `release` revokes a URL once nobody holds it;
 * `revokeAll` (sign-out, switch, page hide, conversation change) revokes every one at once.
 */
export class MediaRegistry {
  #entries = new Map<string, Entry>();
  #urls: UrlApi;
  #clock: () => number;
  #limit: number;

  constructor(urls: UrlApi, options?: { clock?: () => number; limitBytes?: number }) {
    this.#urls = urls;
    this.#clock = options?.clock ?? (() => Date.now());
    this.#limit = options?.limitBytes ?? CACHE_LIMIT_BYTES;
  }

  get size(): number {
    return this.#entries.size;
  }

  get bytes(): number {
    let total = 0;
    for (const entry of this.#entries.values()) total += entry.blob.size;
    return total;
  }

  has(mediaId: string): boolean {
    return this.#entries.has(mediaId);
  }

  /** Holds the blob's URL for `owner`, making it on first use. */
  acquire(mediaId: string, owner: string, blob?: Blob): string | null {
    let entry = this.#entries.get(mediaId);
    if (!entry) {
      if (!blob) return null;
      entry = { url: this.#urls.createObjectURL(blob), blob, owners: new Set(), usedAt: 0 };
      this.#entries.set(mediaId, entry);
    }
    entry.owners.add(owner);
    entry.usedAt = this.#clock();
    this.#evict();
    return entry.url;
  }

  release(mediaId: string, owner: string): void {
    const entry = this.#entries.get(mediaId);
    if (!entry) return;
    entry.owners.delete(owner);
    if (entry.owners.size === 0) this.#drop(mediaId, entry);
  }

  revokeAll(): void {
    for (const [mediaId, entry] of [...this.#entries]) this.#drop(mediaId, entry);
  }

  #drop(mediaId: string, entry: Entry): void {
    this.#urls.revokeObjectURL(entry.url);
    this.#entries.delete(mediaId);
  }

  /** Over the byte bound: the least recently used blobs nobody holds go first, then any. */
  #evict(): void {
    if (this.bytes <= this.#limit) return;
    const byAge = [...this.#entries].sort(
      ([, a], [, b]) => a.owners.size - b.owners.size || a.usedAt - b.usedAt,
    );
    for (const [mediaId, entry] of byAge) {
      if (this.bytes <= this.#limit) return;
      this.#drop(mediaId, entry);
    }
  }
}

/** At most `limit` jobs at a time; the rest wait in order (TH-04: three media fetches per tab). */
export class Gate {
  #running = 0;
  #queue: (() => void)[] = [];
  readonly limit: number;

  constructor(limit = MAX_IN_FLIGHT) {
    this.limit = limit;
  }

  get running(): number {
    return this.#running;
  }

  async run<T>(job: () => Promise<T>): Promise<T> {
    if (this.#running >= this.limit) {
      await new Promise<void>((resolve) => this.#queue.push(resolve));
    }
    this.#running += 1;
    try {
      return await job();
    } finally {
      this.#running -= 1;
      this.#queue.shift()?.();
    }
  }
}

/** The test-only counter of object URLs a journey reads (IJ-05), alive only in a hooks build. */
export interface CountingUrls extends UrlApi {
  created: number;
  revoked: number;
}

export function countingUrls(base: UrlApi): CountingUrls {
  const counter: CountingUrls = {
    created: 0,
    revoked: 0,
    createObjectURL(blob) {
      counter.created += 1;
      return base.createObjectURL(blob);
    },
    revokeObjectURL(url) {
      counter.revoked += 1;
      base.revokeObjectURL(url);
    },
  };
  return counter;
}
