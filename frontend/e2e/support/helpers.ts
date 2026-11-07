/**
 * Helpers for FE-2 onward (TE-02), each exercised once in FE-1 (helpers.spec.ts): the mailbox,
 * the forward signer, fresh emails and a typed API client against the e2e API.
 */
import { randomUUID } from "node:crypto";

import { createApiClient, type WaslaClient } from "@wasla/api-client";
import { signForward, type ForwardHeaders } from "@wasla/session/server";

import { API_ORIGIN, E2E_FORWARD_SECRET, MAILBOX } from "./env";

export interface MailboxMessage {
  to: string;
  subject: string;
  text: string;
  html: string;
  links: string[];
  codes: string[];
  sent_at: string;
}

/** FE-0's mailbox: every email the stack sent, newest first. */
export const mailbox = {
  async messagesTo(address: string): Promise<MailboxMessage[]> {
    const response = await fetch(`${MAILBOX}/messages?to=${encodeURIComponent(address)}`);
    if (!response.ok) throw new Error(`mailbox answered ${response.status}`);
    return (await response.json()) as MailboxMessage[];
  },
  async latestCode(address: string): Promise<string | null> {
    const [latest] = await this.messagesTo(address);
    return latest?.codes[0] ?? null;
  },
};

/** A fresh address per test, so tests never share an account or a bucket. */
export function freshEmail(prefix = "fe"): string {
  return `${prefix}.${randomUUID().slice(0, 12)}@example.com`;
}

/** The three forward headers for one request to the e2e API, signed with its public secret. */
export function signedForward(
  clientIp: string,
  method: string,
  path: string,
  now = Date.now(),
): ForwardHeaders {
  return signForward({ secret: E2E_FORWARD_SECRET, clientIp, method, path, now });
}

/** The typed client (@wasla/api-client) pointed at the e2e API. */
export function e2eApi(getAccessToken: () => string | null = () => null): WaslaClient {
  return createApiClient({ baseUrl: API_ORIGIN, getAccessToken });
}
