/**
 * The event stream's frames (plan Appendix B), hand-written because the document cannot
 * express them (FEB-02 P2). A test holds these lists equal to FE-0's event-types.json (AC-04).
 */

export const EVENT_TYPES = [
  "campaign.changed",
  "conversation.updated",
  "follow_up.updated",
  "message.created",
  "message.status",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export const CLOSE_REASONS = [
  "membership_revoked",
  "server_restart",
  "token_changed",
  "workspace_switched",
  "workspace_unavailable",
] as const;
export type CloseReason = (typeof CLOSE_REASONS)[number];

export const STREAM_FRAMES = ["closed", "resync"] as const;
export type StreamFrame = (typeof STREAM_FRAMES)[number];

export type MessageDirection = "inbound" | "outbound";
export type MessageStatus = "received" | "pending" | "sent" | "delivered" | "read" | "failed";
export type ConversationField = "status" | "owner" | "assignee" | "priority";

export interface MessageCreatedData {
  conversation_id: string;
  message_id: string;
  direction: MessageDirection;
  channel_type: string | null;
  created_at: string;
}

export interface MessageStatusData {
  conversation_id: string;
  message_id: string;
  status: MessageStatus;
}

export interface ConversationUpdatedData {
  conversation_id: string;
  fields: ConversationField[];
}

export interface FollowUpUpdatedData {
  conversation_id: string;
  state: string;
}

export interface CampaignChangedData {
  campaign_id: string;
}

export interface ClosedData {
  reason: CloseReason;
}

/** Every named frame the stream sends, keyed by its SSE `event:` name. */
export interface StreamEventMap {
  "campaign.changed": CampaignChangedData;
  "conversation.updated": ConversationUpdatedData;
  "follow_up.updated": FollowUpUpdatedData;
  "message.created": MessageCreatedData;
  "message.status": MessageStatusData;
  closed: ClosedData;
  resync: Record<string, never>;
}

export type StreamEvent = {
  [K in keyof StreamEventMap]: { type: K; id: string | null; data: StreamEventMap[K] };
}[keyof StreamEventMap];

// Compile-time: the map's keys are exactly the event types plus the control frames.
type Exactly<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
const _keysMatch: Exactly<keyof StreamEventMap, EventType | StreamFrame> = true;
void _keysMatch;

/** The SSE `retry:` the API sends first, and its keep-alive interval (plan Appendix B). */
export const STREAM_RETRY_MS = 3000;
export const STREAM_KEEPALIVE_SECONDS = 15;

export function isEventName(name: string): name is keyof StreamEventMap {
  return (
    (EVENT_TYPES as readonly string[]).includes(name) ||
    (STREAM_FRAMES as readonly string[]).includes(name)
  );
}
