/**
 * FE-4's journey helpers (IJ-01): a customer writing through FE-0's fake inbound WhatsApp webhook
 * (signed with the profile's public test secret), the inbox-state seed helpers
 * (tests/e2e_stack/inbox_states.py through `docker exec`), a connected owner, the API's view of a
 * conversation, and the probes the privacy and media proofs read. Waits are on conditions only.
 */
import { execFile } from "node:child_process";
import { createHmac, randomUUID } from "node:crypto";
import { promisify } from "node:util";

import type { Page } from "@playwright/test";

import { API_ORIGIN } from "./env";
import { verifiedOwner, type Account } from "./fe2";
import { API_CONTAINER, connectNumber, freshNumber } from "./fe3";

const exec = promisify(execFile);

/** The e2e profile's public test value (docker-compose.e2e.yml META_APP_SECRET) - not a secret. */
const META_APP_SECRET = "e2e-meta-app-secret-NOT-A-SECRET";

/** BE1's smoke measured message.created within ~1 s of the webhook; four times a generous 5 s. */
export const ARRIVAL_BOUND_MS = 20_000;

export interface Line {
  owner: Account;
  phoneNumberId: string;
}

/** A verified owner with one connected number (FE-3's fake verifier owns every PNID-E2E… id). */
export async function connectedOwner(prefix = "inbox"): Promise<Line> {
  const owner = await verifiedOwner(prefix);
  const phoneNumberId = freshNumber();
  const connected = await connectNumber(owner, phoneNumberId);
  if (connected.status !== 201 && connected.status !== 200) {
    throw new Error(`connect answered ${connected.status}`);
  }
  return { owner, phoneNumberId };
}

/** A synthetic customer number, nobody's (Egypt's mobile shape, the 5550 test block). */
export function freshCustomer(): string {
  return `2010555${Math.floor(Math.random() * 1e5)
    .toString()
    .padStart(5, "0")}`;
}

/** One inbound message through the fake Meta webhook, signed like Meta signs it. */
export async function inbound(
  line: Line,
  sender: string,
  message: Record<string, unknown>,
  name = "E2E Customer",
): Promise<void> {
  const payload = {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "555000111",
        changes: [
          {
            field: "messages",
            value: {
              messaging_product: "whatsapp",
              metadata: {
                display_phone_number: "201000000000",
                phone_number_id: line.phoneNumberId,
              },
              contacts: [{ profile: { name }, wa_id: sender }],
              messages: [
                {
                  from: sender,
                  id: `wamid.IN${randomUUID().replaceAll("-", "").slice(0, 16)}`,
                  timestamp: String(Math.floor(Date.now() / 1000)),
                  ...message,
                },
              ],
            },
          },
        ],
      },
    ],
  };
  const raw = JSON.stringify(payload);
  const signature = `sha256=${createHmac("sha256", META_APP_SECRET).update(raw).digest("hex")}`;
  const response = await fetch(`${API_ORIGIN}/api/v1/webhooks/whatsapp`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Hub-Signature-256": signature },
    body: raw,
  });
  if (response.status !== 200) throw new Error(`webhook answered ${response.status}`);
}

export const text = (body: string) => ({ type: "text", text: { body } });

export interface ConversationRow {
  id: string;
  /** FE-5: the contact the conversation belongs to. */
  contact_id: string;
  participant: { handle: string | null; display_name: string | null };
  assigned_to_id: string | null;
  unread_count: number;
  status: string;
  mode: string;
}

/**
 * A read with a query string: FE-2's signer covers a decoded path without a query, so reads with
 * parameters go unsigned with the bearer (as FE-3's tenantIdOf does).
 */
export async function read<T>(owner: Account, path: string): Promise<T> {
  const response = await fetch(`${API_ORIGIN}${path}`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return (await response.json()) as T;
}

/** The conversation of one customer number, waited for (a webhook answers before projection). */
export async function conversationOf(owner: Account, sender: string): Promise<ConversationRow> {
  const deadline = Date.now() + ARRIVAL_BOUND_MS;
  while (Date.now() < deadline) {
    const answer = await read<{ items?: ConversationRow[] }>(
      owner,
      "/api/v1/conversations?status=all&limit=100",
    );
    const items = answer.items ?? [];
    const found = items.find((item) => item.participant.handle === `+${sender}`);
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("the conversation did not appear");
}

export async function messagesOf(owner: Account, conversationId: string) {
  const answer = await read<{
    items: Array<{ id: string; body: string | null; direction: string; media: unknown[] }>;
  }>(owner, `/api/v1/conversations/${conversationId}/messages?limit=100`);
  return answer.items;
}

/** The inbox-state seed helper (EC4-04) on FE-0's API container. */
export async function inboxState(...args: string[]): Promise<Record<string, unknown>> {
  const { stdout } = await exec("docker", [
    "exec",
    API_CONTAINER,
    "python",
    "-m",
    "tests.e2e_stack.inbox_states",
    ...args,
  ]);
  return JSON.parse(stdout.trim().split("\n").pop()!) as Record<string, unknown>;
}

/** IJ-05's counter of object URLs (a hooks build only). */
export async function objectUrls(page: Page): Promise<{ created: number; revoked: number } | null> {
  return page.evaluate(() => window.__waslaFe4?.urls() ?? null);
}

/** IJ-10: every place a search term, a phone, a name or message text could have been kept. */
export async function kept(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const storage = (store: Storage) =>
      Array.from({ length: store.length }, (_, index) => {
        const key = store.key(index) ?? "";
        // The composer's draft key is the one place text may live (plan section 21).
        return key.startsWith("wasla-draft:") ? key : `${key}=${store.getItem(key) ?? ""}`;
      }).join("\n");
    const caches = "caches" in window ? (await window.caches.keys()).join(",") : "";
    const databases = (await (indexedDB.databases?.() ?? Promise.resolve([])))
      .map((d) => d.name)
      .join(",");
    return [
      window.location.href,
      JSON.stringify(window.history.state ?? null),
      storage(window.localStorage),
      storage(window.sessionStorage),
      document.cookie,
      caches,
      databases,
    ].join("\n");
  });
}

declare global {
  interface Window {
    __waslaFe4?: { urls: () => { created: number; revoked: number }; registry: () => number };
  }
}
