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

/**
 * UA-02: whether Embedded Signup is offered here - owners and administrators read it; the
 * preview (no API) and a deployment without Meta's configuration answer not enabled.
 */
export function useEmbeddedSignupConfig() {
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  return useQuery({
    queryKey: queryKey(scope, "embedded-signup-config"),
    queryFn: () => unwrap(session.api.GET("/api/v1/whatsapp/embedded-signup/config")),
    enabled: role === "tenant_owner" || role === "tenant_admin",
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}

/** One number's onboarding (OS-02), refreshed by `whatsapp.onboarding.updated` (OS-05). */
export function useOnboarding(id: string, enabled: boolean) {
  const { session } = useSession();
  const { scope } = useWorkspace();
  return useQuery({
    queryKey: queryKey(scope, "whatsapp-onboarding", { id }),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/whatsapp/accounts/{account_id}/onboarding", {
          params: { path: { account_id: id } },
        }),
      ),
    enabled,
    retry: false,
  });
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
