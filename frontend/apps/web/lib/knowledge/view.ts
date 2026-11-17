/**
 * A knowledge base's documents view and its place in the URL (LY6-02): a status from a closed
 * vocabulary and the open document's opaque id - nothing else. Never a title, a search term, a
 * question, a gap query, a file name or a token: the builder has no parameter for them. Unknown
 * values are dropped, never echoed back.
 */
import { isId } from "../customers/view";

export const DOCUMENT_STATUSES = ["pending", "processing", "ready", "failed"] as const;
export type DocumentStatus = (typeof DOCUMENT_STATUSES)[number];

export interface DocumentsView {
  status: DocumentStatus | null;
  /** The document whose panel is open. */
  doc: string | null;
}

export function parseDocumentsView(params: URLSearchParams): DocumentsView {
  const status = params.get("status");
  const doc = params.get("doc");
  return {
    status: (DOCUMENT_STATUSES as readonly string[]).includes(status ?? "")
      ? (status as DocumentStatus)
      : null,
    doc: isId(doc) ? doc.toLocaleLowerCase("en") : null,
  };
}

export function knowledgeBaseHref(baseId: string, view?: Partial<DocumentsView>): string {
  if (!isId(baseId)) return "/knowledge";
  const params = new URLSearchParams();
  if (view?.status && (DOCUMENT_STATUSES as readonly string[]).includes(view.status))
    params.set("status", view.status);
  if (view?.doc && isId(view.doc)) params.set("doc", view.doc);
  const query = params.toString();
  return query ? `/knowledge/${baseId}?${query}` : `/knowledge/${baseId}`;
}

export function agentHref(id: string): string {
  return isId(id) ? `/ai/agents/${id}` : "/ai/agents";
}
