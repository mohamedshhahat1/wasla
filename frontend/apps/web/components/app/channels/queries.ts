"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { queryKey, unwrap } from "@wasla/api-client";

import { useSession } from "../../session-provider";
import { useWorkspace } from "../workspace-context";

/** The reads the channel screens share (one cache entry each, keyed [ws, gen, resource]). */
export function useChannelReads() {
  const { session } = useSession();
  const { scope } = useWorkspace();
  const connections = useQuery({
    queryKey: queryKey(scope, "channel-connections"),
    queryFn: () => unwrap(session.api.GET("/api/v1/channel-connections")),
  });
  const capacity = useQuery({
    queryKey: queryKey(scope, "channel-capacity"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/channel-capacity")),
  });
  const accounts = useQuery({
    queryKey: queryKey(scope, "whatsapp-accounts"),
    queryFn: () => unwrap(session.api.GET("/api/v1/whatsapp/accounts")),
  });
  return { connections, capacity, accounts };
}

/** After every channel action: the lists, the capacity and the gates read again (CN-05, GT-02). */
export function useChannelsChanged() {
  const queryClient = useQueryClient();
  const { scope, recheck } = useWorkspace();
  return useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "channel-connections") }),
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "channel-capacity") }),
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "whatsapp-accounts") }),
      recheck(),
    ]);
  }, [queryClient, scope, recheck]);
}
