/**
 * FE-6's journey helpers (AJ-01): the knowledge-state seed helpers (tests/e2e_stack/
 * knowledge_states.py through `docker exec`), the API's view of agents and documents, writes a
 * second actor makes through the API, and documents generated in memory - never real files. Waits
 * are on conditions only; the bounds come from the fake pipeline's measured time (P-K14) × 4.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { API_ORIGIN } from "./env";
import { testAddress, type Account } from "./fe2";
import { API_CONTAINER } from "./fe3";
import { signedForward } from "./helpers";

const exec = promisify(execFile);

/** 202 → ready on the e2e profile, measured from the smoke's J4 (P-K14), times four. */
export const INDEXING_BOUND_MS = 60_000;

/** The smoke's own document (tests/e2e_stack/smoke.py): obviously fake. */
export const PRICE_TEXT = "The price of a 150 m2 finishing is 500000 EGP.";

export async function call(
  owner: Pick<Account, "token">,
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<{ status: number; json: unknown }> {
  const headers: Record<string, string> = {
    ...signedForward(testAddress(), method, path),
    Authorization: `Bearer ${owner.token}`,
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${API_ORIGIN}${path}`, {
    method,
    headers,
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await response.text();
  return { status: response.status, json: text ? (JSON.parse(text) as unknown) : null };
}

export interface AgentRow {
  id: string;
  name: string;
  status: string;
  is_default: boolean;
  knowledge_unreachable: boolean;
}

export interface DocumentRow {
  id: string;
  title: string;
  status: string;
  error: string | null;
}

export async function agentsOf(owner: Pick<Account, "token">): Promise<AgentRow[]> {
  return (await call(owner, "GET", "/api/v1/agents?limit=100")).json as AgentRow[];
}

export async function documentOf(owner: Pick<Account, "token">, id: string): Promise<DocumentRow> {
  return (await call(owner, "GET", `/api/v1/knowledge/documents/${id}`)).json as DocumentRow;
}

export async function documentsIn(
  owner: Pick<Account, "token">,
  baseId: string,
): Promise<DocumentRow[]> {
  const page = (
    await call(owner, "GET", `/api/v1/knowledge/documents?knowledge_base_id=${baseId}&limit=50`)
  ).json as { items: DocumentRow[] };
  return page.items;
}

/** A knowledge base and a ready text document in it, through the API (the second actor's way). */
export async function baseWithDocument(
  owner: Pick<Account, "token">,
  name: string,
  title = "Prices",
): Promise<{ baseId: string; documentId: string }> {
  const base = await call(owner, "POST", "/api/v1/knowledge/bases", { name });
  if (base.status !== 201) throw new Error(`base answered ${base.status}`);
  const baseId = (base.json as { id: string }).id;
  const doc = await call(owner, "POST", `/api/v1/knowledge/bases/${baseId}/documents`, {
    title,
    content: PRICE_TEXT,
  });
  if (doc.status !== 202) throw new Error(`document answered ${doc.status}`);
  return { baseId, documentId: (doc.json as { document: { id: string } }).document.id };
}

/** Waits until the API says the document is ready or failed (never a fixed sleep). */
export async function settled(owner: Pick<Account, "token">, id: string): Promise<DocumentRow> {
  const until = Date.now() + INDEXING_BOUND_MS;
  for (;;) {
    const row = await documentOf(owner, id);
    if (row.status === "ready" || row.status === "failed") return row;
    if (Date.now() > until) throw new Error(`document ${id} still ${row.status}`);
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

/** An agent made the answering one through the API. */
export async function answeringAgent(
  owner: Pick<Account, "token">,
  name = "Test agent",
): Promise<string> {
  const made = await call(owner, "POST", "/api/v1/agents", {
    name,
    system_prompt: "You answer questions about the test company.",
  });
  if (made.status !== 201) throw new Error(`agent answered ${made.status}`);
  const id = (made.json as { id: string }).id;
  await call(owner, "PATCH", `/api/v1/agents/${id}`, { status: "active" });
  await call(owner, "POST", `/api/v1/agents/${id}/default`);
  return id;
}

/** The knowledge-state seed helper (EC6-04) on FE-0's API container. */
export async function knowledgeState(...args: string[]): Promise<Record<string, unknown>> {
  const { stdout } = await exec("docker", [
    "exec",
    API_CONTAINER,
    "python",
    "-m",
    "tests.e2e_stack.knowledge_states",
    ...args,
  ]);
  return JSON.parse(stdout.trim().split("\n").pop()!) as Record<string, unknown>;
}

/** A file made in memory for the chooser: obviously fake content, the size asked for. */
export function memoryFile(name: string, size: number, mimeType = "text/plain") {
  const line = `${PRICE_TEXT}\n`;
  const text = line.repeat(Math.ceil(size / line.length)).slice(0, size);
  return { name, mimeType, buffer: Buffer.from(text, "utf-8") };
}
