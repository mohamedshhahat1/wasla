/**
 * The activity log's words (AL-01 / AL-02, ADR-201). The API records 146 kinds of action; the page
 * says the ones a workspace's people do or are told about, in words, and shows any other as its
 * code - as text, never as markup. Details are shown only for the fields named here.
 */

/** The actions the catalogue has words for (app.settings.activity.actions.*). */
export const WORDED_ACTIONS = [
  "member_invited",
  "invitation_revoked",
  "invitation_accepted",
  "member_removed",
  "member_left",
  "member_reinstated",
  "workspace_created",
  "workspace_updated",
  "workspace_ownership_transferred",
  "workspace_suspended",
  "workspace_restored",
  "workspace_deleted",
  "conversation_taken_over",
  "conversation_released_to_ai",
  "conversation_assigned",
  "conversation_reassigned",
  "conversation_unassigned",
  "conversation_closed",
  "conversation_reopened",
  "channel_connection_connected",
  "channel_connection_enabled",
  "channel_connection_disabled",
  "channel_connection_released",
  "agent_tool_granted",
  "agent_tool_revoked",
  "knowledge_base_created",
  "knowledge_base_updated",
  "knowledge_base_deleted",
  "knowledge_document_submitted",
  "knowledge_document_deleted",
  "knowledge_settings_updated",
  "campaign_created",
  "campaign_scheduled",
  "campaign_cancelled",
  "campaign_completed",
  "follow_up_settings_updated",
  "contact_renamed",
  "subscription_started",
  "subscription_plan_changed",
  "subscription_cancelled",
] as const;

const WORDED: ReadonlySet<string> = new Set(WORDED_ACTIONS);

export function hasWords(action: string): boolean {
  return WORDED.has(action);
}

/** The before / after fields the screen knows how to say (AL-02); anything else is not shown. */
export const SHOWN_CHANGES = ["role", "name", "slug", "timezone"] as const;

export function changesOf(
  metadata: Record<string, unknown> | null | undefined,
): Array<{ field: (typeof SHOWN_CHANGES)[number]; from: string | null; to: string }> {
  if (!metadata) return [];
  const out: Array<{ field: (typeof SHOWN_CHANGES)[number]; from: string | null; to: string }> = [];
  for (const field of SHOWN_CHANGES) {
    const to = metadata[field];
    if (typeof to !== "string") continue;
    const previous = metadata[`previous_${field}`];
    out.push({ field, from: typeof previous === "string" ? previous : null, to });
  }
  return out;
}

/**
 * The next page's bound: the API pages by `until` (exclusive), newest first, so the next page ends
 * where the last row began. Rows sharing that exact moment would repeat or be skipped at a page
 * edge; the page de-duplicates by id (recorded: the API has no cursor).
 */
export function nextUntil(rows: ReadonlyArray<{ occurred_at: string }>): string | null {
  return rows.length ? rows[rows.length - 1]!.occurred_at : null;
}

export function mergePages<T extends { id: string }>(pages: ReadonlyArray<readonly T[]>): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const page of pages) {
    for (const row of page) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      out.push(row);
    }
  }
  return out;
}
