/**
 * FE-5's journey helpers (CJ-01): the customer-state seed helpers (tests/e2e_stack/
 * customer_states.py through `docker exec`), the API's view of a contact and a lead, and writes
 * a second actor makes through the API. Waits are on conditions only.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { api, type Account } from "./fe2";
import { API_CONTAINER } from "./fe3";
import { ARRIVAL_BOUND_MS, read } from "./fe4";

const exec = promisify(execFile);

export interface ContactRow {
  id: string;
  display_name: string | null;
  name: string | null;
  wa_id: string | null;
  opted_out_at: string | null;
}

export interface LeadRow {
  id: string;
  name: string | null;
  status: string;
  next_statuses: string[];
  score: number;
  assigned_to_id: string | null;
  email: string | null;
  phone: string | null;
  human_verified_fields: string[];
}

/** The contact of one customer number, waited for (a webhook answers before projection). */
export async function contactOf(
  owner: Pick<Account, "token">,
  sender: string,
): Promise<ContactRow> {
  const deadline = Date.now() + ARRIVAL_BOUND_MS;
  while (Date.now() < deadline) {
    const page = await read<{ items: ContactRow[] }>(
      owner as Account,
      `/api/v1/contacts?q=${sender}&limit=10`,
    );
    const found = page.items.find((item) => item.wa_id?.replace("+", "") === sender);
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("the contact did not appear");
}

export async function leadOf(owner: Pick<Account, "token">, id: string): Promise<LeadRow> {
  return read<LeadRow>(owner as Account, `/api/v1/leads/${id}`);
}

export async function createLead(
  owner: Pick<Account, "token">,
  body: Record<string, unknown>,
): Promise<LeadRow> {
  const answer = await api("POST", "/api/v1/leads", { token: owner.token, body });
  if (answer.status !== 201) throw new Error(`create lead answered ${answer.status}`);
  return answer.json as LeadRow;
}

/** A move made by another actor through the API (the stale cases' "someone moved it first"). */
export async function moveLead(
  owner: Pick<Account, "token">,
  id: string,
  status: string,
): Promise<void> {
  const answer = await api("POST", `/api/v1/leads/${id}/status`, {
    token: owner.token,
    body: { status },
  });
  if (answer.status !== 200) throw new Error(`move to ${status} answered ${answer.status}`);
}

/** A lead walked along the pipeline to `status` through the API. */
export async function leadIn(
  owner: Pick<Account, "token">,
  name: string,
  status: "new" | "contacted" | "qualified" | "proposal" | "won" | "lost",
): Promise<LeadRow> {
  const lead = await createLead(owner, { name });
  const path: Record<string, string[]> = {
    new: [],
    contacted: ["contacted"],
    qualified: ["contacted", "qualified"],
    proposal: ["contacted", "qualified", "proposal"],
    won: ["contacted", "qualified", "proposal", "won"],
    lost: ["lost"],
  };
  for (const step of path[status]!) await moveLead(owner, lead.id, step);
  return leadOf(owner, lead.id);
}

/** The customer-state seed helper (EC5-04) on FE-0's API container. */
export async function customerState(...args: string[]): Promise<Record<string, unknown>> {
  const { stdout } = await exec("docker", [
    "exec",
    API_CONTAINER,
    "python",
    "-m",
    "tests.e2e_stack.customer_states",
    ...args,
  ]);
  return JSON.parse(stdout.trim().split("\n").pop()!) as Record<string, unknown>;
}
