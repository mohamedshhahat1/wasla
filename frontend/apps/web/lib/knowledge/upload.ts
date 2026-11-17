/**
 * The upload's pre-checks (KD-03, the plan's FE-6 acceptance): before any request, from
 * `GET /knowledge/limits` (FEB-38) - the server's own per-type rule, never a retyped number. The
 * plan's "5 MiB" is the request body cap; a PDF, workbook or Word file is refused far below it. A
 * file this refuses is never sent. Pure: it reads a name and a size, never the bytes.
 */

export interface SourceLimit {
  source: string;
  extensions: string[];
  media_types: string[];
  max_bytes: number;
}

export interface KnowledgeLimits {
  sources: SourceLimit[];
  max_request_bytes: number;
  max_pdf_pages: number;
  max_characters: number;
  max_title_length: number;
}

export type PreCheck =
  | { ok: true; source: string }
  | { ok: false; reason: "empty" }
  | { ok: false; reason: "type"; extensions: string[] }
  | { ok: false; reason: "size"; source: string; maxBytes: number };

/** The file's extension, lower-cased in a locale-free way (Turkish "I" is not "ı" here). */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLocaleLowerCase("en") : "";
}

export function sourceFor(name: string, limits: KnowledgeLimits): SourceLimit | null {
  const extension = extensionOf(name);
  return limits.sources.find((row) => row.extensions.includes(extension)) ?? null;
}

/** Every extension the server reads, for `accept=` and for the "use one of" sentence. */
export function acceptedExtensions(limits: KnowledgeLimits): string[] {
  return limits.sources.flatMap((row) => row.extensions);
}

export function preCheck(file: { name: string; size: number }, limits: KnowledgeLimits): PreCheck {
  const row = sourceFor(file.name, limits);
  if (!row) return { ok: false, reason: "type", extensions: acceptedExtensions(limits) };
  if (file.size === 0) return { ok: false, reason: "empty" };
  const max = Math.min(row.max_bytes, limits.max_request_bytes);
  if (file.size > max) return { ok: false, reason: "size", source: row.source, maxBytes: max };
  return { ok: true, source: row.source };
}

/** A title from the file's name: without its extension, trimmed to the bound. */
export function titleFromFile(name: string, maxLength: number): string {
  const extension = extensionOf(name);
  const bare = extension ? name.slice(0, name.length - extension.length) : name;
  return Array.from(bare.trim()).slice(0, maxLength).join("");
}

/** The text-document form's own check: something to index, inside the character bound. */
export function textProblem(
  text: string,
  limits: Pick<KnowledgeLimits, "max_characters">,
): "required" | "tooLong" | null {
  if (!text.trim()) return "required";
  if (text.length > limits.max_characters) return "tooLong";
  return null;
}
