/**
 * FE-3's journey helpers (JR-01): staff calls, a member in a second context, the gate states the
 * API cannot reach in a few calls (tests/e2e_stack/gate_states.py through `docker exec` on FE-0's
 * API container), and waits on conditions - never a fixed sleep.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import type { BrowserContext, Page } from "@playwright/test";

import { API_ORIGIN } from "./env";
import { api, PASSWORD, testAddress, waitForCode, waitForLink, type Account } from "./fe2";
import { freshEmail } from "./helpers";

const exec = promisify(execFile);

export const STAFF = {
  email: "staff.e2e@example.com",
  password: "e2e-staff-password-NOT-A-SECRET",
};
export const API_CONTAINER = process.env["WASLA_E2E_API_CONTAINER"] ?? "wasla-fe-e2e-api";

/**
 * The bounds the journeys wait for a gate (section 8): FE-0's smoke measured the closed frame
 * ~6 s after a suspension (J8) and ~12 s after a revocation (J3); four times those.
 */
export const SUSPEND_BOUND_MS = 24_000;
export const REVOKE_BOUND_MS = 48_000;

export async function login(
  email: string,
  password = PASSWORD,
  workspaceSlug?: string,
): Promise<string> {
  const answer = await api("POST", "/api/v1/auth/login", {
    body: { email, password, ...(workspaceSlug ? { workspace_slug: workspaceSlug } : {}) },
  });
  if (answer.status !== 200) throw new Error(`login answered ${answer.status}`);
  return (answer.json as { access_token: string }).access_token;
}

let staff: { token: string; at: number } | null = null;

/**
 * The staff account's bearer, reused for ten minutes (access tokens live fifteen): logging the
 * one staff account in for every suspend meets the per-account log-in limit (429).
 */
export async function staffToken(): Promise<string> {
  if (staff && Date.now() - staff.at < 10 * 60_000) return staff.token;
  staff = { token: await login(STAFF.email, STAFF.password), at: Date.now() };
  return staff.token;
}

export async function tenantIdOf(slug: string, staff: string): Promise<string> {
  // A query string cannot be signed (FE-2's signer): staff reads go unsigned, with the bearer.
  const response = await fetch(
    `${API_ORIGIN}/api/v1/platform/tenants?search=${encodeURIComponent(slug)}`,
    { headers: { Authorization: `Bearer ${staff}` } },
  );
  const items = (
    (await response.json()) as { items: Array<{ tenant: { id: string; slug: string } }> }
  ).items;
  const found = items.find((item) => item.tenant.slug === slug);
  if (!found) throw new Error(`no tenant ${slug}`);
  return found.tenant.id;
}

export async function suspend(slug: string): Promise<void> {
  const staff = await staffToken();
  const id = await tenantIdOf(slug, staff);
  const answer = await api("POST", `/api/v1/platform/tenants/${id}/suspend`, {
    token: staff,
    body: { reason: "e2e FE-3" },
  });
  if (answer.status !== 200) throw new Error(`suspend answered ${answer.status}`);
}

export async function restore(slug: string): Promise<void> {
  const staff = await staffToken();
  const id = await tenantIdOf(slug, staff);
  const answer = await api("POST", `/api/v1/platform/tenants/${id}/restore`, { token: staff });
  if (answer.status !== 200) throw new Error(`restore answered ${answer.status}`);
}

export interface Member {
  email: string;
  token: string;
  userId: string;
}

/** A verified member of `owner`'s workspace (J3's path: invite, accept, verify). */
export async function memberOf(
  owner: Account,
  role: "member" | "tenant_admin" = "member",
): Promise<Member> {
  const email = freshEmail("mate");
  const invited = await api("POST", "/api/v1/invitations", {
    token: owner.token,
    body: { email, role },
  });
  if (invited.status !== 201) throw new Error(`invite answered ${invited.status}`);
  const link = await waitForLink(email, "/invitations/accept");
  const token = new URL(link).searchParams.get("token");
  const accepted = await api("POST", "/api/v1/invitations/accept", {
    address: testAddress(),
    body: { token, password: PASSWORD, full_name: "E2E Mate" },
  });
  if (accepted.status !== 200) throw new Error(`accept answered ${accepted.status}`);
  const bearer = await login(email, PASSWORD, owner.slug);
  // A send supersedes the code sent before it, and the mailbox may hold both: each attempt
  // takes a code it has not tried, and a refused one asks for a new code (FEB-32's race).
  const tried = new Set<string>();
  let verified = { status: 0, json: null as unknown };
  for (let attempt = 0; attempt < 3 && verified.status !== 200; attempt += 1) {
    await api("POST", "/api/v1/auth/email/verification/send", { token: bearer });
    const code = await waitForCode(email, tried);
    tried.add(code);
    verified = await api("POST", "/api/v1/auth/email/verification/verify", {
      token: bearer,
      body: { code },
    });
  }
  if (verified.status !== 200) throw new Error(`verify answered ${verified.status}`);
  const me = await api("GET", "/api/v1/auth/me", { token: bearer });
  return { email, token: bearer, userId: (me.json as { id: string }).id };
}

export async function revoke(owner: Account, member: Member): Promise<void> {
  const answer = await api("DELETE" as "POST", `/api/v1/workspace/members/${member.userId}`, {
    token: owner.token,
  });
  if (answer.status !== 200) throw new Error(`revoke answered ${answer.status}`);
}

/** The test-only seed helper (EC-04): trialing / past-due / ai-allowance-used. */
export async function gateState(
  command: "trialing" | "past-due" | "ai-allowance-used",
  slug: string,
  days?: number,
) {
  const args = [
    "exec",
    API_CONTAINER,
    "python",
    "-m",
    "tests.e2e_stack.gate_states",
    command,
    slug,
  ];
  if (days !== undefined) args.push(String(days));
  const { stdout } = await exec("docker", args);
  return JSON.parse(stdout.trim().split("\n").pop()!) as Record<string, unknown>;
}

/** Connects a number with the fake verifier (every PNID-E2E… id is owned). */
export async function connectNumber(owner: Account, phoneNumberId: string) {
  return api("POST", "/api/v1/whatsapp/accounts", {
    token: owner.token,
    body: {
      phone_number_id: phoneNumberId,
      access_token: "e2e-fake-system-user-token",
      display_name: "E2E line",
    },
  });
}

export function freshNumber(): string {
  return `PNID-E2E-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/** The coordinator's state in a tab (the test hooks of a WASLA_E2E_ROUTES build). */
export async function streamOf(page: Page): Promise<{ role: string; state: string } | null> {
  return page.evaluate(() => window.__waslaFe3?.stream() ?? null);
}

/** Records every URL the browser context requests (the request-count and origin proofs). */
export function recordRequests(context: BrowserContext): string[] {
  const seen: string[] = [];
  context.on("request", (request) => seen.push(`${request.method()} ${request.url()}`));
  return seen;
}

declare global {
  interface Window {
    __waslaFe3?: {
      stream: () => { role: string; state: string };
      mutationVariables: () => unknown[];
    };
    __waslaFe3Flags?: { clockOffsetMs?: number; bypassStreamLock?: boolean };
  }
}
