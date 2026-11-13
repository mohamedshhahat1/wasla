/**
 * What FE-3 does with each stream frame (ST-04). Events invalidate queries, never carry data
 * (plan section 10). FE-3 acts on message.created only - home's conversations card; FE-4 replaces
 * the other entries with the inbox's invalidation map. The table is typed by the event vocabulary
 * (`EVENT_TYPES`, held equal to FE-0's event-types.json), so a new event type that is not handled
 * fails to compile.
 */
import type { QueryClient } from "@tanstack/react-query";

import {
  EVENT_TYPES,
  queryKey,
  type EventType,
  type QueryScope,
  type StreamFrame,
} from "@wasla/api-client";
import type { StreamFrameOut } from "@wasla/api-client/stream";

export interface FrameContext {
  queryClient: QueryClient;
  scope: QueryScope;
}

/** home's conversations card (HM-02). */
export const HOME_CONVERSATIONS = "home-conversations";

const noop = () => undefined;

export const FRAME_HANDLERS: Record<EventType | StreamFrame, (context: FrameContext) => void> = {
  "message.created": ({ queryClient, scope }) => {
    void queryClient.invalidateQueries({ queryKey: queryKey(scope, HOME_CONVERSATIONS) });
  },
  "message.status": noop,
  "conversation.updated": noop,
  "follow_up.updated": noop,
  "campaign.changed": noop,
  // After a gap FE-4 re-reads the inbox; FE-3 has nothing that a gap leaves stale.
  resync: noop,
  // The reasons are the coordinator's and the gates' (ST-03).
  closed: noop,
};

export function handleFrame(frame: StreamFrameOut, context: FrameContext): void {
  const handler = (FRAME_HANDLERS as Record<string, ((c: FrameContext) => void) | undefined>)[
    frame.type
  ];
  handler?.(context);
}

/** For the unit test: every event type of the vocabulary has an entry. */
export const HANDLED_EVENT_TYPES: readonly EventType[] = EVENT_TYPES;
