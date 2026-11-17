// @vitest-environment jsdom
/**
 * FE-6's screens against a recorded API (ADR-200), and FE-5's leftovers: the lead's status read
 * through the real query cache while its move is in flight (M-T41, MC-05); the lead page's one
 * "Tags" (FEF-40); what a member sees of the AI and knowledge screens (absent, not disabled - AG-04,
 * M-U33); the agent page's PATCH and the disable dialog (M-U27, M-U28); home's warning (M-U30); the
 * upload's pre-check sending nothing (M-U21). The session and the workspace are doubles; the
 * components, the query client and the catalogue are the real ones.
 */
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";

import { AgentDetail } from "../components/ai/agent-detail";
import { AgentsList } from "../components/ai/agents-list";
import { Home } from "../components/app/home";
import { LeadStatusControl } from "../components/customers/lead-status";
import type { LeadRead } from "../components/customers/shared";
import { TagEditor } from "../components/customers/tag-editor";
import { BaseDetail } from "../components/knowledge/base-detail";
import { KnowledgeHome } from "../components/knowledge/knowledge-home";
import { leadKey } from "../lib/inbox/keys";

const ME = "11111111-1111-4111-8111-111111111111";
const CONTACT = "22222222-2222-4222-8222-222222222222";
const LEAD = "33333333-3333-4333-8333-333333333333";
const AGENT = "44444444-4444-4444-8444-444444444444";
const BASE = "55555555-5555-4555-8555-555555555555";
const SCOPE = { workspaceId: "ws", sessionGeneration: 1 };

let role: "tenant_owner" | "tenant_admin" | "member" = "member";
type Call = { method: string; path: string; init: Record<string, unknown> | undefined };
let calls: Call[] = [];
let answers: Record<string, (init: Record<string, unknown> | undefined) => unknown> = {};

function answer(method: string, path: string, init?: Record<string, unknown>) {
  calls.push({ method, path, init });
  const make = answers[`${method} ${path}`];
  const result = make ? make(init) : { __status: 404, error: { error: { code: "not_found" } } };
  return Promise.resolve(result).then((value) => {
    const failed = (value as { __status?: number } | null)?.__status;
    return failed
      ? {
          error: (value as { error: unknown }).error,
          response: new Response("{}", { status: failed }),
        }
      : { data: value, response: new Response("{}", { status: 200 }) };
  });
}

const api = {
  GET: (path: string, init?: Record<string, unknown>) => answer("GET", path, init),
  POST: (path: string, init?: Record<string, unknown>) => answer("POST", path, init),
  PATCH: (path: string, init?: Record<string, unknown>) => answer("PATCH", path, init),
  PUT: (path: string, init?: Record<string, unknown>) => answer("PUT", path, init),
  DELETE: (path: string, init?: Record<string, unknown>) => answer("DELETE", path, init),
};

// jsdom has no ResizeObserver; Radix's radio measures itself with one. Nothing here reads sizes.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({
    status: "signed-in",
    profile: { id: ME, full_name: "Test Owner", email: "owner@example.test" },
    profileLoaded: true,
    session: { api },
  }),
}));

vi.mock("../components/app/workspace-context", () => ({
  useWorkspace: () => ({
    workspaceId: "ws",
    workspaceName: "Test workspace",
    scope: SCOPE,
    role,
    gates: { subscription_status: "active" },
    allowance: null,
  }),
}));

const en = messagesFor("en")["app"] as {
  ai: Record<string, unknown> & {
    actions: Record<string, string>;
    status: { sentence: Record<string, string> };
  };
  homeWarnings: Record<string, string>;
  knowledge: Record<string, Record<string, string> | string>;
  customers: { fields: Record<string, string> };
};
const ai = en.ai as unknown as Record<string, string> & {
  actions: Record<string, string>;
  status: { sentence: Record<string, string> };
};

let client: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

function agent(overrides: Record<string, unknown> = {}) {
  return {
    id: AGENT,
    name: "Test agent",
    description: null,
    status: "active",
    model: "model-a",
    system_prompt: "You answer questions about the test company.",
    temperature: 0.3,
    max_output_tokens: 2048,
    memory_message_limit: 20,
    memory_token_budget: 4000,
    is_default: true,
    escalation_sentiment: "angry",
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    knowledge_unreachable: false,
    ...overrides,
  };
}

const OPTIONS = {
  models: ["model-a", "model-b"],
  default_model: "model-a",
  max_output_tokens: { default: 2048, ceiling: 2048 },
  bounds: {
    name: { max_length: 200 },
    description: { max_length: 500 },
    system_prompt: { max_length: 20000 },
    temperature: { min: 0, max: 2 },
    memory_message_limit: { min: 1, max: 200 },
    memory_token_budget: { min: 200, max: 100000 },
  },
  default_knowledge_grant: true,
};

function lead(overrides: Partial<LeadRead> = {}): LeadRead {
  return {
    id: LEAD,
    contact_id: CONTACT,
    conversation_id: null,
    name: "Nour Test",
    phone: null,
    email: null,
    interest: null,
    budget_amount: null,
    budget_currency: null,
    status: "new",
    next_statuses: ["contacted", "lost"],
    source: "manual",
    score: 10,
    assigned_to_id: null,
    tags: [],
    custom_fields: {},
    human_verified_fields: [],
    qualified_at: null,
    closed_at: null,
    last_activity_at: null,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  role = "member";
  calls = [];
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  answers = {
    "GET /api/v1/agents": () => [agent()],
    "GET /api/v1/agents/options": () => OPTIONS,
    "GET /api/v1/agents/{agent_id}": () => agent(),
    "GET /api/v1/agents/available-tools": () => [
      { name: "search_knowledge", description: "x", parameters: {} },
    ],
    "GET /api/v1/agents/{agent_id}/tools": () => [],
    "GET /api/v1/workspace/members": () => ({ members: [] }),
    "GET /api/v1/conversations": () => ({ items: [], next_cursor: null }),
    "GET /api/v1/channel-connections": () => [],
    "GET /api/v1/leads/statistics": () => ({
      total: 0,
      open_leads: 0,
      unassigned: 0,
      by_status: {},
    }),
    "GET /api/v1/usage": () => ({
      counters: { messages_received: 0, messages_sent: 0, ai_turns: 0 },
    }),
    "GET /api/v1/knowledge/documents": () => ({ items: [], next_cursor: null }),
  };
});

afterEach(() => cleanup());

const sent = (method: string, path: string) =>
  calls.filter((call) => call.method === method && call.path === path);

describe("M-T41 (MC-05): a lead's status through the real cache waits for the answer", () => {
  /** The lead page's flow: the lead read from the cache, drawn by the control. */
  function CachedLead() {
    const read = useQuery({
      queryKey: leadKey(SCOPE, LEAD),
      queryFn: () => lead(),
      staleTime: Number.POSITIVE_INFINITY,
    });
    return read.data ? <LeadStatusControl lead={read.data} /> : null;
  }

  it("pending: the cache and the badge keep the stored status; the answer replaces it", async () => {
    let finish: (value: unknown) => void = () => undefined;
    answers["POST /api/v1/leads/{lead_id}/status"] = () =>
      new Promise((resolve) => {
        finish = resolve;
      });
    client.setQueryData(leadKey(SCOPE, LEAD), lead());
    render(<CachedLead />, { wrapper: Wrapper });
    const trigger = await screen.findByRole("button", { name: /change/i });
    fireEvent(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    const [contacted] = await screen.findAllByRole("menuitem");
    fireEvent.click(contacted!);
    await waitFor(() => expect(sent("POST", "/api/v1/leads/{lead_id}/status")).toHaveLength(1));
    // In flight: nothing optimistic in the cache, nor on the screen.
    expect((client.getQueryData(leadKey(SCOPE, LEAD)) as LeadRead).status).toBe("new");
    expect(document.querySelector("[data-lead-status]")?.getAttribute("data-lead-status")).toBe(
      "new",
    );
    await act(() => {
      finish(
        lead({
          status: "contacted",
          next_statuses: ["qualified", "lost"] as LeadRead["next_statuses"],
        }),
      );
      return Promise.resolve();
    });
    await waitFor(() =>
      expect(document.querySelector("[data-lead-status]")?.getAttribute("data-lead-status")).toBe(
        "contacted",
      ),
    );
    expect((client.getQueryData(leadKey(SCOPE, LEAD)) as LeadRead).status).toBe("contacted");
  });

  it("a refused move leaves the cache as it was", async () => {
    answers["POST /api/v1/leads/{lead_id}/status"] = () => ({
      __status: 409,
      error: { error: { code: "stale_lead_status", message: "x" } },
    });
    answers["GET /api/v1/leads/{lead_id}"] = () => lead();
    client.setQueryData(leadKey(SCOPE, LEAD), lead());
    render(<CachedLead />, { wrapper: Wrapper });
    const trigger = await screen.findByRole("button", { name: /change/i });
    fireEvent(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    const [contacted] = await screen.findAllByRole("menuitem");
    fireEvent.click(contacted!);
    await waitFor(() => expect(sent("GET", "/api/v1/leads/{lead_id}")).toHaveLength(1));
    expect((client.getQueryData(leadKey(SCOPE, LEAD)) as LeadRead).status).toBe("new");
  });
});

describe("FEF-40: the tag editor inside a titled section says 'Tags' once (M-U40)", () => {
  it("one visible label, and the input keeps its accessible name", () => {
    render(
      <section aria-label="Lead fields">
        <h3>{en.customers.fields["tags"]}</h3>
        <TagEditor tags={["vip"]} onChange={() => undefined} labelHidden />
      </section>,
      { wrapper: Wrapper },
    );
    const label = document.querySelector("[data-tag-editor] label");
    expect(label?.className).toContain("sr-only");
    expect(screen.getByRole("textbox", { name: en.customers.fields["tags"]! })).toBeTruthy();
  });
  it("on its own it still shows its label", () => {
    render(<TagEditor tags={[]} onChange={() => undefined} />, { wrapper: Wrapper });
    expect(document.querySelector("[data-tag-editor] label")?.className).not.toContain("sr-only");
  });
});

describe("AG-02 / AG-04: a member reads and sees no control (absent, not disabled)", () => {
  it("the agents list has no 'New agent'", async () => {
    render(<AgentsList />, { wrapper: Wrapper });
    await screen.findByText("Test agent");
    expect(screen.queryByRole("button", { name: ai["newAgent"]! })).toBeNull();
  });
  it("the agent page draws text and no input, switch or Save; only GETs are sent", async () => {
    render(<AgentDetail id={AGENT} />, { wrapper: Wrapper });
    await screen.findByText(ai.status.sentence["answering"]!);
    expect(document.querySelector("[data-agent-form]")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: ai["save"]! })).toBeNull();
    expect(screen.queryByRole("button", { name: ai.actions["disable"]! })).toBeNull();
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });
});

describe("OQ-3 (M-U33): /knowledge for a member sends no knowledge request", () => {
  it("the knowledge home and a base page show the role state", () => {
    render(<KnowledgeHome />, { wrapper: Wrapper });
    expect(screen.getByText(en.knowledge["notForRole"] as string)).toBeTruthy();
    cleanup();
    render(<BaseDetail id={BASE} />, { wrapper: Wrapper });
    expect(calls.filter((call) => call.path.startsWith("/api/v1/knowledge"))).toEqual([]);
  });
});

describe("the agent page for an administrator", () => {
  beforeEach(() => {
    role = "tenant_admin";
  });
  it("a PATCH carries exactly the changed field (M-U28)", async () => {
    answers["PATCH /api/v1/agents/{agent_id}"] = (init) =>
      agent((init?.["body"] ?? {}) as Record<string, unknown>);
    render(<AgentDetail id={AGENT} />, { wrapper: Wrapper });
    const field = (await screen.findAllByRole("textbox")).find(
      (input) => input.getAttribute("data-field") === "temperature",
    )!;
    fireEvent.change(field, { target: { value: "0.7" } });
    fireEvent.click(screen.getByRole("button", { name: ai["save"]! }));
    await waitFor(() => expect(sent("PATCH", "/api/v1/agents/{agent_id}")).toHaveLength(1));
    expect(sent("PATCH", "/api/v1/agents/{agent_id}")[0]!.init?.["body"]).toEqual({
      temperature: 0.7,
    });
  });
  it("disabling the answering agent asks first and sends nothing until confirmed (M-U27)", async () => {
    answers["PATCH /api/v1/agents/{agent_id}"] = () => agent({ status: "disabled" });
    render(<AgentDetail id={AGENT} />, { wrapper: Wrapper });
    fireEvent.click(await screen.findByRole("button", { name: ai.actions["disable"]! }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(ai["disableBody"] as string)).toBeTruthy();
    expect(sent("PATCH", "/api/v1/agents/{agent_id}")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: ai["disableConfirm"]! }));
    await waitFor(() => expect(sent("PATCH", "/api/v1/agents/{agent_id}")).toHaveLength(1));
    expect(sent("PATCH", "/api/v1/agents/{agent_id}")[0]!.init?.["body"]).toEqual({
      status: "disabled",
    });
  });
  it("'Make default' is not offered on a draft (M-U26)", async () => {
    answers["GET /api/v1/agents/{agent_id}"] = () => agent({ status: "draft", is_default: false });
    render(<AgentDetail id={AGENT} />, { wrapper: Wrapper });
    await screen.findByRole("button", { name: ai.actions["activate"]! });
    expect(screen.queryByRole("button", { name: ai.actions["makeDefault"]! })).toBeNull();
  });
});

describe("HM6-01: home warns until an ACTIVE default exists (M-U30)", () => {
  it("a disabled default: every role is told; a member gets the member's sentence", async () => {
    answers["GET /api/v1/agents"] = () => [agent({ status: "disabled" })];
    render(<Home />, { wrapper: Wrapper });
    await screen.findByText(en.homeWarnings["noAgentTitle"]!);
    expect(screen.getByText(en.homeWarnings["noAgentMember"]!)).toBeTruthy();
  });
  it("an active default: no warning", async () => {
    render(<Home />, { wrapper: Wrapper });
    await waitFor(() => expect(sent("GET", "/api/v1/agents")).toHaveLength(1));
    await act(() => Promise.resolve());
    expect(screen.queryByText(en.homeWarnings["noAgentTitle"]!)).toBeNull();
  });
});
