"use client";

/**
 * Who is who in the inbox (IL-03, PN-01): the customer's words from the participant, the members'
 * names from GET /workspace/members, the connected numbers from FE-3's channel reads. One request
 * each per workspace, cached under FE-3's keys.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { queryKey, unwrap, type components } from "@wasla/api-client";

import { membersKey } from "../../lib/inbox/keys";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";

export type ConversationRead = components["schemas"]["ConversationRead"];
export type MessageRead = components["schemas"]["MessageRead"];
export type MediaRead = NonNullable<MessageRead["media"]>[number];

export function useMembers() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  const members = useQuery({
    queryKey: membersKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace/members")),
    staleTime: 5 * 60_000,
  });
  return useMemo(() => {
    const byId = new Map<string, { name: string; email: string }>();
    for (const member of members.data?.members ?? []) {
      if (member.status !== "active") continue;
      byId.set(member.user_id, { name: member.full_name || member.email, email: member.email });
    }
    return { query: members, byId };
  }, [members]);
}

export function useNumbers() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  const connections = useQuery({
    queryKey: queryKey(scope, "channel-connections"),
    queryFn: () => unwrap(session.api.GET("/api/v1/channel-connections")),
    staleTime: 5 * 60_000,
  });
  const accounts = useQuery({
    queryKey: queryKey(scope, "whatsapp-accounts"),
    queryFn: () => unwrap(session.api.GET("/api/v1/whatsapp/accounts")),
    staleTime: 5 * 60_000,
  });
  return useMemo(() => {
    const labels = new Map<string, string>();
    for (const account of accounts.data?.accounts ?? []) {
      if (account.display_phone_number) labels.set(account.id, account.display_phone_number);
    }
    const many = (connections.data?.length ?? 0) > 1;
    /** The connected number a conversation came through - only when there is more than one. */
    const numberOf = (connectionId: string): string | null =>
      many ? (labels.get(connectionId) ?? null) : null;
    return { numberOf };
  }, [connections.data, accounts.data]);
}

/** IL-03: the name, else the handle, else null (the screen says "Unknown customer"). */
export function whoOf(conversation: ConversationRead): string | null {
  return conversation.participant.display_name || conversation.participant.handle || null;
}

/** The handle as a second line, only when the name is the first one. */
export function handleLine(conversation: ConversationRead): string | null {
  return conversation.participant.display_name ? (conversation.participant.handle ?? null) : null;
}
