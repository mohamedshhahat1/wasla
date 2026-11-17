/**
 * FE-6's journeys (AJ-02 .. AJ-10) against FE-0's profile: the plan's acceptance through the
 * screens - J4 (upload → pending → indexing → ready → a customer asks → the AI answers from the
 * document), the upload's pre-checks (nothing sent), polling that stops at ready / failed, and
 * home's warning until an ACTIVE default exists. Every step waits on a fact - a response with its
 * status, a row, a read-back through the API - never a press having happened or a fixed sleep.
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page, Request, Response } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import {
  label,
  logInThroughTheApp,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
} from "../support/fe2";
import { memberOf, recordRequests } from "../support/fe3";
import {
  ARRIVAL_BOUND_MS,
  connectedOwner,
  freshCustomer,
  inbound,
  kept,
  text,
} from "../support/fe4";
import {
  agentsOf,
  answeringAgent,
  baseWithDocument,
  call,
  documentOf,
  documentsIn,
  INDEXING_BOUND_MS,
  knowledgeState,
  memoryFile,
  PRICE_TEXT,
  settled,
} from "../support/fe6";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 300_000 });

const AI = "app.ai";
const KB = "app.knowledge";
const HOME = "app.homeWarnings";
/** What the scripted agent answers after a knowledge search (tests/e2e_stack/smoke.py). */
const SEARCH_REPLY = "Here is what I found.";

async function axe(page: Page, state: string): Promise<void> {
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(page.locator('[role="dialog"][data-state="closed"]')).toHaveCount(0);
  await expect(page.locator('button[aria-busy="true"]')).toHaveCount(0);
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every(
        (animation) =>
          animation.playState !== "running" ||
          animation.effect?.getComputedTiming().iterations === Infinity,
      ),
  );
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"])
    .analyze();
  test.info().annotations.push({
    type: `axe ${state}`,
    description: JSON.stringify(result.violations.map((v) => [v.id, v.impact, v.nodes.length])),
  });
  expect(
    result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map(
        (v) =>
          `${state}: ${v.id} ${v.help} | ${v.nodes
            .map(
              (node) => `${node.target.join(" ")} (${node.any.map((c) => c.message).join("; ")})`,
            )
            .join(" || ")}`,
      ),
  ).toEqual([]);
}

async function signIn(page: Page, context: BrowserContext, locale: Locale, email: string) {
  await useAddress(context);
  await useLocale(context, locale);
  await logInThroughTheApp(page, locale, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
}

function answerOf(
  page: Page,
  method: string,
  path: RegExp,
  bound = ARRIVAL_BOUND_MS,
): Promise<Response> {
  return page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        url.origin === API_ORIGIN &&
        response.request().method() === method &&
        path.test(url.pathname)
      );
    },
    { timeout: bound },
  );
}

/** Every API request the page sends, with its time (the polling evidence, AJ-04). */
function apiLog(
  page: Page,
): Array<{ at: number; method: string; path: string; body: string | null }> {
  const seen: Array<{ at: number; method: string; path: string; body: string | null }> = [];
  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (url.origin !== API_ORIGIN) return;
    seen.push({
      at: Date.now(),
      method: request.method(),
      path: url.pathname + url.search,
      body: request.postData(),
    });
  });
  return seen;
}

for (const locale of ["en", "ar"] as const) {
  test(`AJ-02 / AJ-03 (${locale}): an owner creates an agent, edits it, makes it answer; home warns until then`, async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner(`ai${locale}`);
    await signIn(page, context, locale, owner.email);
    const log = apiLog(page);

    // A new workspace answers nothing (plan J2): home says so, with the way to fix it.
    const warning = page.locator('[data-banner="no-answering-agent"]');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText(label(locale, `${HOME}.noAgentTitle`));
    await axe(page, `home no agent ${locale}`);
    await warning.getByRole("link", { name: label(locale, `${HOME}.noAgentAction`) }).click();
    await page.waitForURL(`${WEB}/ai/agents`);
    await expect(page.getByText(label(locale, `${AI}.empty`))).toBeVisible();
    await axe(page, `agents empty ${locale}`);

    // New agent: one POST and no grant from the screen (the API grants search_knowledge itself).
    await page.getByRole("button", { name: label(locale, `${AI}.emptyAction`) }).click();
    const dialog = page.getByRole("dialog");
    await dialog.locator('[data-field="agent-name"]').fill("Test agent");
    await dialog
      .locator('[data-field="agent-instructions"]')
      .fill("You answer questions about the test company.");
    const created = answerOf(page, "POST", /\/api\/v1\/agents$/);
    await dialog.getByRole("button", { name: label(locale, `${AI}.create.submit`) }).click();
    expect((await created).status()).toBe(201);
    await page.waitForURL(/\/ai\/agents\/[0-9a-f-]{36}$/);
    await expect(page.locator('[data-status-sentence="draft"]')).toBeVisible();
    expect(log.filter((entry) => entry.method === "PUT")).toEqual([]);
    expect(
      log.filter((entry) => entry.method === "POST" && entry.path === "/api/v1/agents"),
    ).toHaveLength(1);
    await axe(page, `agent draft ${locale}`);

    // Two fields changed → a PATCH with exactly those two keys; "Off" → null.
    await page
      .locator('[data-field="system_prompt"]')
      .fill("You answer briefly about the test company.");
    await page.locator('[data-field="temperature"]').fill("0.5");
    const patched = answerOf(page, "PATCH", /\/api\/v1\/agents\/[0-9a-f-]{36}$/);
    await page.locator('[data-action="save-agent"]').click();
    const patch = await patched;
    expect(patch.status()).toBe(200);
    expect(
      Object.keys(JSON.parse(patch.request().postData() ?? "{}") as Record<string, unknown>).sort(),
    ).toEqual(["system_prompt", "temperature"]);
    await page.getByRole("radio", { name: label(locale, `${AI}.fields.escalationOff`) }).click();
    const off = answerOf(page, "PATCH", /\/api\/v1\/agents\/[0-9a-f-]{36}$/);
    await page.locator('[data-action="save-agent"]').click();
    expect(JSON.parse((await off).request().postData() ?? "{}")).toEqual({
      escalation_sentiment: null,
    });

    // Draft: no "Make default". Activate, then make it the default.
    await expect(page.locator('[data-action="makeDefault"]')).toHaveCount(0);
    const activated = answerOf(page, "PATCH", /\/api\/v1\/agents\/[0-9a-f-]{36}$/);
    await page.locator('[data-action="activate"]').click();
    expect((await activated).status()).toBe(200);
    const made = answerOf(page, "POST", /\/default$/);
    await page.locator('[data-action="makeDefault"]').click();
    expect((await made).status()).toBe(200);
    await expect(page.locator('[data-status-sentence="answering"]')).toBeVisible();
    await expect(page.locator("[data-answers]").first()).toBeVisible();
    await axe(page, `agent answering ${locale}`);

    // Home: no warning, without a reload of home - its agents read was invalidated.
    await page
      .getByRole("link", { name: label(locale, "app.nav.home") })
      .first()
      .click();
    await page.waitForURL(`${WEB}/`);
    await expect(page.locator('[data-banner="no-answering-agent"]')).toHaveCount(0);
    const agents = await agentsOf(owner);
    expect(agents.filter((agent) => agent.is_default && agent.status === "active")).toHaveLength(1);

    // Disabling the answering agent asks first; confirmed, home warns again.
    await page.goto(`${WEB}/ai/agents/${agents[0]!.id}`);
    await page.locator('[data-action="disable"]').click();
    const confirm = page.getByRole("dialog");
    await expect(confirm).toContainText(label(locale, `${AI}.disableBody`));
    await axe(page, `disable dialog ${locale}`);
    const disabled = answerOf(page, "PATCH", /\/api\/v1\/agents\/[0-9a-f-]{36}$/);
    await confirm.locator('[data-action="confirm-disable"]').click();
    expect(JSON.parse((await disabled).request().postData() ?? "{}")).toEqual({
      status: "disabled",
    });
    await expect(page.locator('[data-banner="agent-default-off"]')).toBeVisible();
    await page.goto(`${WEB}/`);
    await expect(page.locator('[data-banner="no-answering-agent"]')).toBeVisible();

    // AJ-08: no instruction text anywhere the page could have kept it.
    expect(await kept(page)).not.toContain("You answer briefly");
    expect(page.url()).not.toContain("answer");
  });
}

test("AJ-02 / AJ-09 (member): reads the agent, sees no control; no Knowledge entry, no knowledge request", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("aimem");
  const agentId = await answeringAgent(owner);
  const member = await memberOf(owner);
  const seen = recordRequests(context);
  await signIn(page, context, "en", member.email);
  await expect(page.getByRole("link", { name: label("en", "app.nav.ai") }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: label("en", "app.nav.knowledge") })).toHaveCount(0);
  await page.goto(`${WEB}/ai/agents/${agentId}`);
  await expect(page.locator("[data-read-only]")).toBeVisible();
  await expect(page.getByRole("switch")).toHaveCount(0);
  await expect(page.locator('[data-action="save-agent"]')).toHaveCount(0);
  await expect(page.locator('[data-action="disable"]')).toHaveCount(0);
  await axe(page, "agent read-only");
  await page.goto(`${WEB}/knowledge`);
  await expect(page.getByText(label("en", `${KB}.notForRole`))).toBeVisible();
  expect(seen.filter((entry) => /\/api\/v1\/knowledge/.test(entry))).toEqual([]);
  expect(
    seen.filter((entry) => !entry.startsWith("GET ") && /\/api\/v1\/agents/.test(entry)),
  ).toEqual([]);
});

test("AJ-04: upload → waiting → indexing → ready on screen; polling stops; a customer is answered from it (J4)", async ({
  page,
  context,
}) => {
  const line = await connectedOwner("kb");
  await answeringAgent(line.owner);
  await signIn(page, context, "en", line.owner.email);
  const log = apiLog(page);
  await page
    .getByRole("link", { name: label("en", "app.nav.knowledge") })
    .first()
    .click();
  await page.waitForURL(`${WEB}/knowledge`);
  await axe(page, "knowledge empty");
  await page.locator('[data-action="new-base"]').click();
  await page.getByRole("dialog").locator('[data-field="base-name"]').fill("Facts");
  const base = answerOf(page, "POST", /\/api\/v1\/knowledge\/bases$/);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: label("en", `${KB}.create.submit`) })
    .click();
  expect((await base).status()).toBe(201);
  await page.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/);

  await page.locator('[data-action="upload"]').click();
  const upload = page.getByRole("dialog");
  await upload
    .locator("input[type=file]")
    .setInputFiles(memoryFile("prices.txt", PRICE_TEXT.length + 1));
  await expect(upload.locator('[data-field="document-title"]')).toHaveValue("prices");
  await axe(page, "upload dialog");
  const sent = answerOf(page, "POST", /\/documents\/upload$/);
  await upload.getByRole("button", { name: label("en", `${KB}.upload.submit`) }).click();
  const accepted = await sent;
  expect(accepted.status()).toBe(202);
  const documentId = ((await accepted.json()) as { document: { id: string } }).document.id;

  // The row moves on its own: the list is polled while it is not ready.
  const row = page.locator("[data-document-row]").first();
  await expect(row.locator("[data-document-status]")).toHaveAttribute(
    "data-document-status",
    "ready",
    {
      timeout: INDEXING_BOUND_MS,
    },
  );
  const seen = new Set(
    log
      .filter((entry) => entry.path.startsWith("/api/v1/knowledge/documents?"))
      .map((entry) => entry.path),
  );
  expect(seen.size).toBeGreaterThanOrEqual(1);
  // The plan's "stops at ready": no knowledge request for 20 s after the last row became ready.
  const quietFrom = Date.now();
  await expect
    .poll(() => Date.now() - quietFrom, { timeout: 30_000, intervals: [5_000] })
    .toBeGreaterThan(20_000);
  expect(
    log.filter((entry) => entry.at > quietFrom && entry.path.startsWith("/api/v1/knowledge")),
  ).toEqual([]);
  await axe(page, "documents ready");
  expect((await documentOf(line.owner, documentId)).status).toBe("ready");

  // J4 through the screens: the customer asks, the AI answers from the document.
  const customer = freshCustomer();
  await inbound(line, customer, text("What is the price?"));
  await page.goto(`${WEB}/inbox`);
  await page.locator("[data-conversation-row]").first().click();
  await expect(page.getByText(SEARCH_REPLY).first()).toBeVisible({ timeout: INDEXING_BOUND_MS });

  // AJ-08: the title and the text were never kept.
  const stored = await kept(page);
  expect(stored).not.toContain("prices");
  expect(stored).not.toContain("500000");
});

test("AJ-05: pre-checks send nothing; a duplicate is replaced; a failed document retries; replace and delete", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("kbpre");
  const { baseId, documentId } = await baseWithDocument(owner, "Facts");
  await settled(owner, documentId);
  await signIn(page, context, "en", owner.email);
  const log = apiLog(page);
  await page.goto(`${WEB}/knowledge/${baseId}`);
  await expect(page.locator("[data-document-row]")).toHaveCount(1);

  // A type the server does not read, and a PDF one byte over its own limit: refused, nothing sent.
  const limits = (await call(owner, "GET", "/api/v1/knowledge/limits")).json as {
    sources: Array<{ source: string; max_bytes: number }>;
  };
  const pdfMax = limits.sources.find((row) => row.source === "pdf")!.max_bytes;
  await page.locator('[data-action="upload"]').click();
  const dialog = page.getByRole("dialog");
  const file = dialog.locator("input[type=file]");
  await file.setInputFiles(memoryFile("setup.exe", 10, "application/octet-stream"));
  await expect(dialog.getByRole("alert")).toContainText(".pdf");
  await file.setInputFiles(memoryFile("prices.pdf", pdfMax + 1, "application/pdf"));
  await expect(dialog.getByRole("alert")).toBeVisible();
  await file.setInputFiles(memoryFile("empty.txt", 0));
  await expect(dialog.getByRole("alert")).toContainText(label("en", `${KB}.upload.emptyRefused`));
  await expect(
    dialog.getByRole("button", { name: label("en", `${KB}.upload.submit`) }),
  ).toBeDisabled();
  await axe(page, "upload refused");
  expect(
    log.filter((entry) => entry.method === "POST" && entry.path.includes("/documents/upload")),
  ).toEqual([]);

  // The same source again: "already exists" → replace it.
  await file.setInputFiles(memoryFile("Prices.txt", PRICE_TEXT.length));
  await dialog.locator('[data-field="document-title"]').fill("Prices");
  const first = answerOf(page, "POST", /\/documents\/upload$/);
  await dialog.getByRole("button", { name: label("en", `${KB}.upload.submit`) }).click();
  const answered = await first;
  if (answered.status() === 409) {
    await expect(dialog.locator("[data-duplicate]")).toBeVisible();
    const replaced = answerOf(page, "POST", /\/documents\/upload$/);
    await dialog.locator('[data-action="replace-existing"]').click();
    expect((await replaced).url()).toContain("replace=true");
  }
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // A failed document (EC6-04): its reason in the panel, polling stopped, "Retry" starts it again.
  const doc = (await documentsIn(owner, baseId))[0]!;
  await settled(owner, doc.id);
  const failed = await knowledgeState("failed", doc.id);
  expect(failed["status"]).toBe("failed");
  await page.reload();
  await page.locator("[data-document-row]").first().getByRole("button").click();
  await expect(page.locator('[data-banner="document-error"]')).toContainText("(e2e)");
  await axe(page, "panel failed");
  const retried = answerOf(page, "POST", /\/ingest$/);
  await page.locator('[data-action="retry-indexing"]').click();
  expect((await retried).status()).toBe(202);

  // Delete: asks first, then the row is gone.
  await settled(owner, doc.id);
  await page.locator('[data-action="delete-document"]').click();
  const removed = answerOf(page, "DELETE", /\/api\/v1\/knowledge\/documents\/[0-9a-f-]{36}$/);
  await page.locator('[data-action="confirm-delete-document"]').click();
  expect((await removed).status()).toBe(204);
  await expect(page.locator("[data-document-row]")).toHaveCount(0);
});

test("AJ-06: a base with documents needs its name typed; answer settings send one key; the scope", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("kbdel");
  const agentId = await answeringAgent(owner);
  const kept1 = await baseWithDocument(owner, "Kept");
  const gone = await baseWithDocument(owner, "Gone");
  await settled(owner, gone.documentId);
  await signIn(page, context, "en", owner.email);

  // The scope: only "Kept", saved as ids.
  await page.goto(`${WEB}/ai/agents/${agentId}`);
  await page.getByRole("radio", { name: label("en", `${AI}.tools.scope.chosen`) }).click();
  await page.getByRole("checkbox", { name: "Kept" }).click();
  const scoped = answerOf(page, "PUT", /\/tools$/);
  await page.locator('[data-action="save-scope"]').click();
  expect(JSON.parse((await scoped).request().postData() ?? "{}")).toEqual({
    name: "search_knowledge",
    enabled: true,
    config: { knowledge_base_ids: [kept1.baseId] },
  });

  // A base with documents: 409, then the typed confirmation, then cascade.
  await page.goto(`${WEB}/knowledge/${gone.baseId}`);
  await expect(page.locator("[data-document-row]")).toHaveCount(1);
  await page.locator('[data-action="delete-base"]').click();
  const first = answerOf(page, "DELETE", /\/api\/v1\/knowledge\/bases\/[0-9a-f-]{36}$/);
  await page.locator('[data-action="confirm-delete-base"]').click();
  expect((await first).status()).toBe(409);
  const typed = page.getByRole("dialog");
  await expect(typed).toContainText(label("en", `${KB}.delete.fullTitle`));
  await axe(page, "typed confirmation");
  const confirm = typed.getByRole("button", { name: label("en", `${KB}.delete.confirm`) });
  await typed.getByRole("textbox").fill("Wrong name");
  await expect(confirm).toBeDisabled();
  await typed.getByRole("textbox").fill("Gone");
  const cascade = answerOf(page, "DELETE", /\/api\/v1\/knowledge\/bases\/[0-9a-f-]{36}$/);
  await confirm.click();
  const deleted = await cascade;
  expect(deleted.url()).toContain("cascade=true");
  expect(deleted.status()).toBe(204);
  await page.waitForURL(`${WEB}/knowledge`);

  // Answer settings: one key, then back to the default with null.
  await page.locator('[data-disclosure="answer-settings"] button[aria-expanded]').click();
  await page
    .getByRole("radio", { name: label("en", `${KB}.settings.number_grounding.strict`) })
    .click();
  const strict = answerOf(page, "PATCH", /\/knowledge\/settings$/);
  expect(JSON.parse((await strict).request().postData() ?? "{}")).toEqual({
    number_grounding: "strict",
  });
  const reset = answerOf(page, "PATCH", /\/knowledge\/settings$/);
  await page
    .getByRole("button", { name: label("en", `${KB}.settings.useDefault`) })
    .first()
    .click();
  expect(
    Object.values(
      JSON.parse((await reset).request().postData() ?? "{}") as Record<string, unknown>,
    ),
  ).toEqual([null]);
  await axe(page, "settings");
});

test("AJ-07: test a question shows passages as text; the gaps are switched off; nothing kept", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("kbtest");
  const { documentId } = await baseWithDocument(owner, "Facts");
  await settled(owner, documentId);
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/knowledge`);
  await page.locator('[data-disclosure="test-question"] button[aria-expanded]').click();
  await page.locator('[data-field="test-question"]').fill("What is the price of finishing?");
  const searched = answerOf(page, "POST", /\/knowledge\/search$/);
  await page.getByRole("button", { name: label("en", `${KB}.search.submit`) }).click();
  expect((await searched).status()).toBe(200);
  await expect(page.locator("[data-search-result]")).toBeVisible();
  await axe(page, "search result");
  await page.locator('[data-disclosure="unanswered"] button[aria-expanded]').click();
  await expect(page.locator("[data-gaps-off]")).toBeVisible();
  await axe(page, "gaps off");
  expect(page.url()).toBe(`${WEB}/knowledge`);
  expect(await kept(page)).not.toContain("price of finishing");
});

test("AJ-08: knowledge_unreachable is told to owners on home", async ({ page, context }) => {
  const owner = await verifiedOwner("kbunr");
  const agentId = await answeringAgent(owner);
  const { documentId } = await baseWithDocument(owner, "Facts");
  await settled(owner, documentId);
  await call(owner, "DELETE", `/api/v1/agents/${agentId}/tools/search_knowledge`);
  await expect.poll(async () => (await agentsOf(owner))[0]?.knowledge_unreachable).toBe(true);
  await signIn(page, context, "en", owner.email);
  await expect(page.locator('[data-banner="knowledge-unreachable"]')).toBeVisible();
  await axe(page, "home unreachable");
});
