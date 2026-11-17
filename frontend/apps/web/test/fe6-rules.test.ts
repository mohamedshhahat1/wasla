/**
 * FE-6's pure rules (ADR-200): who answers customers and what each status offers (AG-05, AG-06,
 * HM6-01), what an agent's PATCH carries (AG-04), the knowledge scope (TL-03), the upload's
 * pre-checks (KD-03), the polling back-off (PO-02) and what may live in a URL (LY6-02). Each is the
 * unit test a mutant of section 23 must meet (M-U22 .. M-U31, M-U36, M-U38).
 */
import { describe, expect, it } from "vitest";

import {
  agentPatch,
  agentProblems,
  escalationOf,
  formOf,
  isDirty,
  knowledgeScopeConfig,
  type AgentOptions,
  type AgentShape,
} from "../lib/ai/agent-form";
import {
  actionBody,
  actionsFor,
  answeringAgent,
  answers,
  needsDisableConfirmation,
  noAnsweringAgent,
  statusSentence,
} from "../lib/ai/agent-status";
import {
  isTerminal,
  needsPolling,
  newlySettled,
  nextInterval,
  POLL_FIRST_MS,
  POLL_MAX_MS,
  reindexing,
  statusFingerprint,
} from "../lib/knowledge/poll";
import {
  acceptedExtensions,
  extensionOf,
  preCheck,
  textProblem,
  titleFromFile,
  type KnowledgeLimits,
} from "../lib/knowledge/upload";
import { agentHref, knowledgeBaseHref, parseDocumentsView } from "../lib/knowledge/view";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7";

const AGENT: AgentShape = {
  name: "Test agent",
  description: null,
  system_prompt: "You answer questions about the test company.",
  model: "model-a",
  temperature: 0.3,
  max_output_tokens: 2048,
  memory_message_limit: 20,
  memory_token_budget: 4000,
  escalation_sentiment: "angry",
};

const OPTIONS: AgentOptions = {
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

describe("AG-05: who answers, and what each status offers", () => {
  const draft = { id: ID, status: "draft", is_default: false };
  const idle = { id: ID, status: "active", is_default: false };
  const answering = { id: ID, status: "active", is_default: true };
  const disabledDefault = { id: ID, status: "disabled", is_default: true };

  it("only an active default answers (M-U30)", () => {
    expect(answers(answering)).toBe(true);
    expect(answers(disabledDefault)).toBe(false);
    expect(answers(idle)).toBe(false);
    expect(answeringAgent([draft, disabledDefault])).toBeNull();
  });
  it("'Make default' only for an active agent that is not the default (M-U26)", () => {
    expect(actionsFor(draft)).toEqual(["activate"]);
    expect(actionsFor(idle)).toEqual(["makeDefault", "disable"]);
    expect(actionsFor(answering)).toEqual(["disable"]);
    expect(actionsFor(disabledDefault)).toEqual(["activate"]);
    expect(actionsFor({ status: "disabled", is_default: false })).not.toContain("makeDefault");
  });
  it("only disabling the answering agent asks first (M-U27)", () => {
    expect(needsDisableConfirmation(answering, "disable")).toBe(true);
    expect(needsDisableConfirmation(idle, "disable")).toBe(false);
    expect(needsDisableConfirmation(idle, "makeDefault")).toBe(false);
  });
  it("each state has its sentence", () => {
    expect([draft, idle, answering, disabledDefault].map(statusSentence)).toEqual([
      "draft",
      "activeIdle",
      "answering",
      "disabledDefault",
    ]);
    expect(statusSentence({ status: "disabled", is_default: false })).toBe("disabled");
  });
  it("an action's body is a status PATCH, and making default is its own request", () => {
    expect(actionBody("activate")).toEqual({ status: "active" });
    expect(actionBody("disable")).toEqual({ status: "disabled" });
    expect(actionBody("makeDefault")).toBeNull();
  });
});

describe("HM6-01: why nobody answers", () => {
  it("no agent, or none default → none", () => {
    expect(noAnsweringAgent([])).toEqual({ kind: "none" });
    expect(noAnsweringAgent([{ id: ID, status: "active", is_default: false }])).toEqual({
      kind: "none",
    });
  });
  it("a disabled default is named so it can be turned back on (M-U30)", () => {
    expect(noAnsweringAgent([{ id: ID, status: "disabled", is_default: true }])).toEqual({
      kind: "defaultInactive",
      agentId: ID,
    });
  });
  it("an answering agent → no warning", () => {
    expect(noAnsweringAgent([{ id: ID, status: "active", is_default: true }])).toBeNull();
  });
});

describe("AG-04: an agent's PATCH carries only what changed", () => {
  it("nothing changed → an empty body and not dirty", () => {
    expect(agentPatch(AGENT, formOf(AGENT))).toEqual({});
    expect(isDirty(AGENT, formOf(AGENT))).toBe(false);
  });
  it("two fields changed → exactly those two keys (M-U28)", () => {
    const values = { ...formOf(AGENT), system_prompt: "New instructions.", temperature: "0.7" };
    expect(agentPatch(AGENT, values)).toEqual({
      system_prompt: "New instructions.",
      temperature: 0.7,
    });
  });
  it("escalation 'off' is sent as null - a value, not an absence (M-U29)", () => {
    const patch = agentPatch(AGENT, { ...formOf(AGENT), escalation: "off" });
    expect(Object.keys(patch)).toEqual(["escalation_sentiment"]);
    expect(patch.escalation_sentiment).toBeNull();
    expect(escalationOf(null)).toBe("off");
    expect(escalationOf("positive")).toBe("off");
  });
  it("a description cannot be removed, only changed", () => {
    const withOne = { ...AGENT, description: "Sales." };
    const values = { ...formOf(withOne), description: "  " };
    expect(agentPatch(withOne, values)).toEqual({});
    expect(agentProblems(values, withOne, OPTIONS).description).toBe("descriptionCannotBeRemoved");
  });
  it("Arabic-Indic digits are read as numbers", () => {
    expect(agentPatch(AGENT, { ...formOf(AGENT), memory_message_limit: "٣٠" })).toEqual({
      memory_message_limit: 30,
    });
  });
  it("the bounds are the deployment's own", () => {
    const over = { ...formOf(AGENT), max_output_tokens: "2049", temperature: "2.5" };
    const problems = agentProblems(over, AGENT, OPTIONS);
    expect(problems.max_output_tokens).toBe("outOfRange");
    expect(problems.temperature).toBe("outOfRange");
    expect(
      agentProblems({ ...formOf(AGENT), memory_message_limit: "1.5" }, AGENT, OPTIONS)
        .memory_message_limit,
    ).toBe("notAWholeNumber");
    expect(agentProblems({ ...formOf(AGENT), model: "model-z" }, AGENT, OPTIONS).model).toBe(
      "modelNotOffered",
    );
    expect(
      agentProblems({ ...formOf(AGENT), system_prompt: "x".repeat(20001) }, AGENT, OPTIONS)
        .system_prompt,
    ).toBe("tooLong");
    expect(agentProblems(formOf(AGENT), AGENT, OPTIONS)).toEqual({});
  });
});

describe("TL-03: search_knowledge's scope", () => {
  it("every base is null, never an empty list (M-U38)", () => {
    expect(knowledgeScopeConfig("all", [ID])).toBeNull();
    expect(knowledgeScopeConfig("chosen", [ID, ID, OTHER])).toEqual({
      knowledge_base_ids: [ID, OTHER],
    });
  });
});

const LIMITS: KnowledgeLimits = {
  sources: [
    { source: "text", extensions: [".txt"], media_types: ["text/plain"], max_bytes: 1_600_000 },
    {
      source: "markdown",
      extensions: [".md", ".markdown"],
      media_types: ["text/plain"],
      max_bytes: 1_600_000,
    },
    { source: "pdf", extensions: [".pdf"], media_types: ["application/pdf"], max_bytes: 300_000 },
  ],
  max_request_bytes: 5 * 1024 * 1024,
  max_pdf_pages: 40,
  max_characters: 400_000,
  max_title_length: 300,
};

describe("KD-03: nothing is sent that the server would refuse for its type or size", () => {
  it("an extension the server does not read is refused, with the list (M-U21)", () => {
    expect(preCheck({ name: "setup.exe", size: 10 }, LIMITS)).toEqual({
      ok: false,
      reason: "type",
      extensions: [".txt", ".md", ".markdown", ".pdf"],
    });
  });
  it("a PDF one byte over its own limit is refused - far below the 5 MiB body cap (M-U22)", () => {
    expect(preCheck({ name: "prices.pdf", size: 300_001 }, LIMITS)).toEqual({
      ok: false,
      reason: "size",
      source: "pdf",
      maxBytes: 300_000,
    });
    expect(preCheck({ name: "prices.pdf", size: 300_000 }, LIMITS)).toEqual({
      ok: true,
      source: "pdf",
    });
  });
  it("a text file at its limit is accepted; an empty file is refused", () => {
    expect(preCheck({ name: "prices.txt", size: 1_600_000 }, LIMITS)).toEqual({
      ok: true,
      source: "text",
    });
    expect(preCheck({ name: "prices.txt", size: 0 }, LIMITS)).toEqual({
      ok: false,
      reason: "empty",
    });
  });
  it("the body cap still bounds a per-type limit above it", () => {
    const loose = { ...LIMITS, max_request_bytes: 1000 };
    expect(preCheck({ name: "a.txt", size: 1001 }, loose)).toMatchObject({
      reason: "size",
      maxBytes: 1000,
    });
  });
  it("extensions are read without the page's locale (Turkish I)", () => {
    expect(extensionOf("PRICES.PDF")).toBe(".pdf");
    expect(extensionOf("FIYATLAR.TXT")).toBe(".txt");
    expect(extensionOf(".hidden")).toBe("");
    expect(acceptedExtensions(LIMITS)).toContain(".markdown");
  });
  it("a title from the file name, inside its bound", () => {
    expect(titleFromFile("Price list.pdf", 300)).toBe("Price list");
    expect(titleFromFile("a".repeat(400) + ".txt", 300)).toHaveLength(300);
  });
  it("a text document needs text inside the character bound", () => {
    expect(textProblem("  ", LIMITS)).toBe("required");
    expect(textProblem("x".repeat(400_001), LIMITS)).toBe("tooLong");
    expect(textProblem("Opening hours: 9 to 5.", LIMITS)).toBeNull();
  });
});

describe("PO-02: polling backs off, resets on a change and stops at ready or failed", () => {
  it("3 s, ×1.5 while nothing changes, capped at 15 s (M-U25)", () => {
    const seen: number[] = [];
    let interval: number | null = null;
    for (let poll = 0; poll < 7; poll += 1) {
      interval = nextInterval(interval, false);
      seen.push(interval);
    }
    expect(seen).toEqual([3000, 4500, 6750, 10125, 15000, 15000, 15000]);
    expect(Math.max(...seen)).toBe(POLL_MAX_MS);
  });
  it("a change resets the interval to 3 s (M-U25)", () => {
    expect(nextInterval(15000, true)).toBe(POLL_FIRST_MS);
  });
  it("ready with nothing in flight and failed are terminal; the rest poll (M-U23)", () => {
    expect(isTerminal({ id: ID, status: "ready" })).toBe(true);
    expect(isTerminal({ id: ID, status: "failed" })).toBe(true);
    expect(isTerminal({ id: ID, status: "pending" })).toBe(false);
    expect(isTerminal({ id: ID, status: "processing" })).toBe(false);
    const rebuilding = { id: ID, status: "ready", indexing: { state: "processing" } };
    expect(reindexing(rebuilding)).toBe(true);
    expect(isTerminal(rebuilding)).toBe(false);
    expect(isTerminal({ id: ID, status: "ready", indexing: { state: "failed" } })).toBe(true);
    expect(
      needsPolling([
        { id: ID, status: "ready" },
        { id: OTHER, status: "failed" },
      ]),
    ).toBe(false);
    expect(
      needsPolling([
        { id: ID, status: "ready" },
        { id: OTHER, status: "pending" },
      ]),
    ).toBe(true);
  });
  it("a fingerprint changes with a status and one announcement counts what settled", () => {
    const before = [
      { id: ID, status: "processing" },
      { id: OTHER, status: "pending" },
    ];
    const after = [
      { id: ID, status: "ready" },
      { id: OTHER, status: "failed" },
    ];
    expect(statusFingerprint(before)).not.toBe(statusFingerprint(after));
    expect(newlySettled(before, after)).toEqual({ ready: 1, failed: 1 });
    expect(newlySettled(after, after)).toEqual({ ready: 0, failed: 0 });
  });
});

describe("LY6-02: the documents view in the URL carries ids and a status only", () => {
  it("unknown values are dropped, never echoed (M-U36)", () => {
    expect(parseDocumentsView(new URLSearchParams("status=ready&doc=" + ID))).toEqual({
      status: "ready",
      doc: ID,
    });
    expect(
      parseDocumentsView(new URLSearchParams("status=prices&doc=Price%20list&q=hours&title=x")),
    ).toEqual({ status: null, doc: null });
  });
  it("the builder has no parameter for text", () => {
    expect(knowledgeBaseHref(ID, { status: "failed", doc: OTHER })).toBe(
      `/knowledge/${ID}?status=failed&doc=${OTHER}`,
    );
    expect(knowledgeBaseHref(ID, { doc: "Price list" })).toBe(`/knowledge/${ID}`);
    expect(knowledgeBaseHref("not-an-id")).toBe("/knowledge");
    expect(agentHref("x")).toBe("/ai/agents");
    expect(agentHref(ID)).toBe(`/ai/agents/${ID}`);
  });
});
