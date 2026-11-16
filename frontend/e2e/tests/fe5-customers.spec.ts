/**
 * FE-5's customer journeys (CJ-02 .. CJ-12) against FE-0's profile. A customer writes through the
 * fake inbound webhook; every step waits on a fact - a response with its status, a row, a
 * read-back through the API - never on a press having happened and never a fixed sleep (CJ-01).
 * No journey stops the API.
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page, Response } from "@playwright/test";

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
import { memberOf, recordRequests, revoke, suspend, SUSPEND_BOUND_MS } from "../support/fe3";
import {
  ARRIVAL_BOUND_MS,
  connectedOwner,
  conversationOf,
  freshCustomer,
  inbound,
  kept,
  read,
  text,
} from "../support/fe4";
import { contactOf, createLead, customerState, leadIn, leadOf, moveLead } from "../support/fe5";
import { actorContext, expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 300_000 });

const C = "app.customers";
const BOUND = ARRIVAL_BOUND_MS;

async function axe(page: Page, state: string): Promise<void> {
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
      .map((v) => `${state}: ${v.id} ${v.help}`),
  ).toEqual([]);
}

async function signIn(page: Page, context: BrowserContext, locale: Locale, email: string) {
  await useAddress(context);
  await useLocale(context, locale);
  await logInThroughTheApp(page, locale, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
}

/** The answer of the API call an action sends: the fact a step waits on. */
function answerOf(page: Page, method: string, path: RegExp): Promise<Response> {
  return page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        url.origin === API_ORIGIN &&
        response.request().method() === method &&
        path.test(url.pathname)
      );
    },
    { timeout: BOUND },
  );
}

async function openMove(page: Page, locale: Locale, status: string) {
  // The fact first: the menu of the move before this one is gone and the control is at rest.
  const trigger = page.locator("[data-status-trigger]").first();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger).toBeEnabled();
  await trigger.click();
  const key = `${C}.status.moveTo.${status}`;
  const item = page.getByRole("menuitem", { name: label(locale, key) });
  await expect(item).toBeVisible();
  return item;
}

for (const locale of ["en", "tr"] as const) {
  test(`CJ-02 (${locale}): contacts list, Turkish folding in every search, nothing kept`, async ({
    page,
    context,
  }) => {
    const line = await connectedOwner("cus");
    const ipek = freshCustomer();
    const nour = freshCustomer();
    await inbound(line, ipek, text("Merhaba"), "İpek Işık Test");
    await inbound(line, nour, text("Hello"), "Nour Test");
    const ipekConversation = await conversationOf(line.owner, ipek);
    await conversationOf(line.owner, nour);
    await createLead(line.owner, { name: "Istanbul Test Lead" });
    await createLead(line.owner, { name: "Cairo Test Lead" });

    await signIn(page, context, locale, line.owner.email);
    await page.goto(`${WEB}/contacts`);
    await expect(page.locator("[data-contact-row]")).toHaveCount(2, { timeout: BOUND });
    if (locale === "en") await axe(page, "contacts with rows");

    for (const term of ["ipek", "IPEK", "isik"]) {
      const searched = answerOf(page, "GET", /\/api\/v1\/contacts$/);
      await page.locator("[data-customers-search]").fill(term);
      expect((await searched).status()).toBe(200);
      await expect(page.locator("[data-contact-row]")).toHaveCount(1, { timeout: BOUND });
      await expect(page.locator("[data-contact-row]")).toContainText("İpek Işık Test");
      expect(page.url()).toBe(`${WEB}/contacts`);
    }
    expect((await kept(page)).toLowerCase()).not.toContain("ipek");
    await page.locator("[data-customers-search]").fill("zzz-no-such-customer");
    await expect(page.locator("[data-contact-row]")).toHaveCount(0, { timeout: BOUND });
    if (locale === "en") await axe(page, "contacts no match");

    // The leads search folds the same way, and neither its term nor a tag reaches the URL.
    await page.goto(`${WEB}/leads`);
    await expect(page.locator("[data-lead-row]")).toHaveCount(2, { timeout: BOUND });
    await page.locator("[data-customers-search]").fill("ıstanbul");
    await expect(page.locator("[data-lead-row]")).toHaveCount(1, { timeout: BOUND });
    await expect(page.locator("[data-lead-row]")).toContainText("Istanbul Test Lead");
    expect(page.url()).toBe(`${WEB}/leads`);
    expect(await kept(page)).not.toContain("stanbul");

    // And the inbox search (BE1's q) through the same predicate.
    await page.goto(`${WEB}/inbox`);
    await expect(page.locator('[data-page="inbox"]')).toBeVisible({ timeout: 30_000 });
    await page.locator("[data-inbox-search]").fill("ISIK");
    await expect(
      page.locator(`[data-conversation-row^="/inbox/${ipekConversation.id}"]`).first(),
    ).toBeVisible({ timeout: BOUND });
    await expect(page.locator("[data-conversation-row]")).toHaveCount(1, { timeout: BOUND });
  });
}

test("CJ-03 / CJ-04: a member names a customer and records a stop; an owner clears it; the history", async ({
  page,
  context,
  browser,
}) => {
  const line = await connectedOwner("cus");
  const member = await memberOf(line.owner);
  const sender = freshCustomer();
  await inbound(line, sender, text("Hello"), "Nour Test");
  const conversation = await conversationOf(line.owner, sender);
  const contact = await contactOf(line.owner, sender);

  await signIn(page, context, "en", member.email);
  await page.goto(`${WEB}/contacts/${contact.id}`);
  await expect(page.locator('[data-screen="contact"]')).toBeVisible({ timeout: BOUND });
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Nour Test");
  await axe(page, "contact detail");

  // The name: PATCH, then the heading, then the API's conversation (what the inbox reads).
  await page.getByRole("button", { name: label("en", `${C}.contact.giveName`) }).click();
  const field = page.locator("[data-inline-edit] input, form[data-inline-edit] input").first();
  await field.fill("Nour (VIP) Test");
  const renamed = answerOf(page, "PATCH", /\/api\/v1\/contacts\/[^/]+$/);
  await field.press("Enter");
  expect((await renamed).status()).toBe(200);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Nour (VIP) Test");
  await expect(page.locator("[data-profile-name]")).toContainText("Nour Test");
  await expect
    .poll(
      async () =>
        (
          await read<{ participant: { display_name: string | null } }>(
            line.owner,
            `/api/v1/conversations/${conversation.id}`,
          )
        ).participant.display_name,
      { timeout: BOUND },
    )
    .toBe("Nour (VIP) Test");
  // The conversation is listed and opens the inbox.
  await expect(
    page.locator(`[data-conversation-row^="/inbox/${conversation.id}"]`).first(),
  ).toBeVisible({ timeout: BOUND });

  // A member records the stop; the member is offered no clear.
  await page.locator("[data-record-opt-out]").click();
  await axe(page, "record opt-out dialog");
  const recorded = answerOf(page, "POST", /\/opt-out$/);
  await page.locator("[data-confirm-opt-out]").click();
  expect((await recorded).status()).toBe(200);
  await expect(page.locator('[data-consent-state="opted-out"]')).toBeVisible({ timeout: BOUND });
  await expect(page.locator("[data-consent-history] li")).toHaveCount(1, { timeout: BOUND });
  await expect(page.locator("[data-clear-opt-out]")).toHaveCount(0);
  // The scan waits for the dialog to be gone: WebKit measured text through its fading scrim
  // (frontend.yml 43882980521).
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await axe(page, "contact opted out");

  // The owner clears it, deliberately.
  const ownerContext = await actorContext(browser);
  const ownerPage = await ownerContext.newPage();
  await signIn(ownerPage, ownerContext, "en", line.owner.email);
  await ownerPage.goto(`${WEB}/contacts/${contact.id}`);
  await ownerPage.locator("[data-clear-opt-out]").click();
  await expect(ownerPage.locator("[data-confirm-clear]")).toBeDisabled();
  await ownerPage.getByRole("checkbox").click();
  const cleared = answerOf(ownerPage, "DELETE", /\/opt-out$/);
  await ownerPage.locator("[data-confirm-clear]").click();
  expect((await cleared).status()).toBe(200);
  await expect(ownerPage.locator('[data-consent-state="receiving"]')).toBeVisible({
    timeout: BOUND,
  });
  await expect(ownerPage.locator("[data-consent-history] li")).toHaveCount(2, { timeout: BOUND });

  // The customer's own stop word is a third row, by the customer.
  await inbound(line, sender, text("STOP"), "Nour Test");
  await expect
    .poll(async () => (await contactOf(line.owner, sender)).opted_out_at !== null, {
      timeout: BOUND,
    })
    .toBe(true);
  await ownerPage.reload();
  await expect(ownerPage.locator("[data-consent-history] li")).toHaveCount(3, { timeout: BOUND });
  await expect(ownerPage.locator("[data-consent-history] li").first()).toContainText(
    label("en", `${C}.consent.event.optedOutByCustomer`),
  );
  expect(await kept(ownerPage)).not.toContain(sender);
  await ownerContext.close();
});

test("CJ-03b: an opt-out older than the history is one marked row", async ({ page, context }) => {
  const line = await connectedOwner("cus");
  const sender = freshCustomer();
  await inbound(line, sender, text("Hello"), "Old Stop Test");
  const contact = await contactOf(line.owner, sender);
  await customerState("old-opt-out", contact.id);
  await signIn(page, context, "en", line.owner.email);
  await page.goto(`${WEB}/contacts/${contact.id}`);
  await expect(page.locator("[data-consent-history] li")).toHaveCount(1, { timeout: BOUND });
  await expect(page.locator("[data-consent-history] li")).toContainText(
    label("en", `${C}.consent.event.beforeHistory`),
  );
});

test("CJ-05 .. CJ-08 (owner): filters in the URL, statistics, new lead, fields, status, score, owner, notes", async ({
  page,
  context,
}) => {
  const line = await connectedOwner("lead");
  const member = await memberOf(line.owner);
  for (const status of ["new", "contacted", "qualified", "proposal", "won", "lost"] as const) {
    await leadIn(line.owner, `Lead ${status} Test`, status);
  }
  await signIn(page, context, "en", line.owner.email);
  const statistics = answerOf(page, "GET", /\/api\/v1\/leads\/statistics$/);
  await page.goto(`${WEB}/leads`);
  expect((await statistics).status()).toBe(200);
  await expect(page.locator("[data-lead-statistics]")).toBeVisible({ timeout: BOUND });
  await expect(page.locator("[data-lead-row]")).toHaveCount(6, { timeout: BOUND });
  await axe(page, "leads with the strip");

  // A filter lives in the URL and survives a reload; Back returns to the unfiltered list.
  await page.goto(`${WEB}/leads?status=won`);
  await expect(page.locator("[data-lead-row]")).toHaveCount(1, { timeout: BOUND });
  await expect(page.locator("[data-lead-row]")).toContainText("Lead won Test");
  await page.reload();
  await expect(page.locator("[data-lead-row]")).toHaveCount(1, { timeout: BOUND });
  await page.goto(`${WEB}/leads?assignee=none&source=manual`);
  await expect(page.locator("[data-lead-row]")).toHaveCount(6, { timeout: BOUND });
  await page.goto(`${WEB}/leads?status=archived&search=x`);
  await expect(page.locator("[data-lead-row]")).toHaveCount(6, { timeout: BOUND });

  // New lead: "500k" never leaves the browser; a plain number creates the lead.
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/leads$/.test(new URL(request.url()).pathname)) {
      posts.push(request.postData() ?? "");
    }
  });
  await page.locator("[data-new-lead]").click();
  const form = page.locator("[data-lead-create]");
  await form.getByRole("textbox").first().fill("Dina Test");
  await form.locator("[data-budget]").fill("500k");
  await form.getByRole("button", { name: label("en", `${C}.create.submit`) }).click();
  await expect(form.getByText(label("en", `${C}.problems.budget`))).toBeVisible();
  await axe(page, "new lead dialog with an error");
  expect(posts).toHaveLength(0);
  await form.locator("[data-budget]").fill("500000");
  const created = answerOf(page, "POST", /\/api\/v1\/leads$/);
  await form.getByRole("button", { name: label("en", `${C}.create.submit`) }).click();
  const answer = await created;
  expect(answer.status()).toBe(201);
  const lead = (await answer.json()) as { id: string };
  await page.waitForURL(`${WEB}/leads/${lead.id}`);
  await expect(page.locator('[data-screen="lead"]')).toBeVisible({ timeout: BOUND });
  expect(posts).toHaveLength(1);
  await axe(page, "lead detail");

  // Fields: only what changed is sent; an emptied field is null; both are then confirmed.
  const patched = page.waitForRequest(
    (request) => request.method() === "PATCH" && request.url().includes(`/leads/${lead.id}`),
  );
  await page.locator('[data-field="email"]').fill("dina@example.test");
  await page.locator('[data-section="name"] [data-save-section]').click();
  expect((await patched).postDataJSON()).toEqual({ email: "dina@example.test" });
  await expect
    .poll(async () => (await leadOf(line.owner, lead.id)).email, { timeout: BOUND })
    .toBe("dina@example.test");
  const emptied = page.waitForRequest(
    (request) => request.method() === "PATCH" && request.url().includes(`/leads/${lead.id}`),
  );
  await page.locator('[data-field="name"]').fill("");
  await page.locator('[data-section="name"] [data-save-section]').click();
  expect((await emptied).postDataJSON()).toEqual({ name: null });
  await expect
    .poll(async () => (await leadOf(line.owner, lead.id)).human_verified_fields, {
      timeout: BOUND,
    })
    .toEqual(expect.arrayContaining(["email", "name"]));
  await expect(page.locator("[data-field-confirmed]").first()).toBeVisible({ timeout: BOUND });

  // Status: the menu is next_statuses; a move names the status the page shows.
  const stored = await leadOf(line.owner, lead.id);
  await page.locator("[data-status-trigger]").first().click();
  await expect(page.getByRole("menuitem")).toHaveCount(stored.next_statuses.length);
  const moving = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith(`/leads/${lead.id}/status`),
  );
  const moved = answerOf(page, "POST", /\/status$/);
  await page.getByRole("menuitem", { name: label("en", `${C}.status.moveTo.contacted`) }).click();
  expect((await moving).postDataJSON()).toEqual({ status: "contacted", expected_status: "new" });
  expect((await moved).status()).toBe(200);
  await expect(page.locator('[data-lead-status="contacted"]')).toBeVisible({ timeout: BOUND });

  // A stale move: a colleague moves it first; the page reloads the lead and re-sends nothing.
  await moveLead(line.owner, lead.id, "qualified");
  const moves: number[] = [];
  page.on("response", (response) => {
    if (response.url().endsWith(`/leads/${lead.id}/status`)) moves.push(response.status());
  });
  const stale = answerOf(page, "POST", /\/status$/);
  await (await openMove(page, "en", "lost")).click();
  expect((await stale).status()).toBe(409);
  await expect(page.locator('[data-move-notice="stale"]')).toBeVisible({ timeout: BOUND });
  await expect(page.locator('[data-lead-status="qualified"]')).toBeVisible({ timeout: BOUND });
  expect(moves).toEqual([409]);
  expect((await leadOf(line.owner, lead.id)).status).toBe("qualified");
  await axe(page, "lead with the stale notice");

  // Score: saved, and the stored value is the one typed.
  const scored = answerOf(page, "POST", /\/score$/);
  await page.locator("[data-score-field] input").fill("70");
  await page.locator("[data-score-field] button[type=submit]").click();
  expect((await scored).status()).toBe(200);
  expect((await leadOf(line.owner, lead.id)).score).toBe(70);

  // The owner assigns the lead, naming the owner the page shows (null).
  const assigning = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/assignment"),
  );
  const assigned = answerOf(page, "POST", /\/assignment$/);
  await page.locator('[data-section="owner"]').getByRole("combobox").click();
  await page.getByRole("option", { name: "E2E Mate" }).click();
  expect((await assigning).postDataJSON()).toEqual({
    assigned_to_id: member.userId,
    expected_assigned_to_id: null,
  });
  expect((await assigned).status()).toBe(200);
  expect((await leadOf(line.owner, lead.id)).assigned_to_id).toBe(member.userId);

  // A note: text with its line break, a link that asks first, no edit and no delete.
  const noted = answerOf(page, "POST", /\/notes$/);
  await page
    .locator("[data-note-composer] textarea")
    .fill("اتصل بعد الخامسة\nhttps://example.test/quote <b>x</b>");
  await page.locator("[data-note-composer] button[type=submit]").click();
  expect((await noted).status()).toBe(201);
  const note = page.locator("[data-note]").first();
  await expect(note).toContainText("اتصل بعد الخامسة", { timeout: BOUND });
  await expect(note.locator("b")).toHaveCount(0);
  await expect(note.locator("a")).toHaveCount(0);
  await expect(page.locator("[data-timeline-entry]").first()).toBeVisible({ timeout: BOUND });
  const text0 = await kept(page);
  expect(text0).not.toContain("dina@example.test");
  expect(text0).not.toContain("example.test/quote");
});

test("CJ-05 / CJ-08 (member): no statistics request, no owner control", async ({
  page,
  context,
}) => {
  const line = await connectedOwner("lead");
  const member = await memberOf(line.owner);
  const lead = await createLead(line.owner, { name: "Member View Test" });
  await signIn(page, context, "en", member.email);
  const seen = recordRequests(context);
  const listed = answerOf(page, "GET", /\/api\/v1\/leads$/);
  await page.goto(`${WEB}/leads`);
  expect((await listed).status()).toBe(200);
  await expect(page.locator("[data-lead-row]")).toHaveCount(1, { timeout: BOUND });
  await expect(page.locator("[data-lead-statistics]")).toHaveCount(0);
  await axe(page, "leads for a member");
  await page.locator("[data-lead-row] a").first().click();
  await page.waitForURL(`${WEB}/leads/${lead.id}`);
  await expect(page.locator("[data-owner-text]")).toBeVisible({ timeout: BOUND });
  await expect(page.locator('[data-section="owner"]').getByRole("combobox")).toHaveCount(0);
  expect(seen.filter((url) => url.includes("/leads/statistics"))).toEqual([]);
  expect(seen.filter((entry) => /^GET \S+\/api\/v1\/leads(\?|$)/.test(entry))).toHaveLength(1);

  // An unknown lead is the not-found state, never an error page.
  await page.goto(`${WEB}/leads/00000000-0000-4000-8000-000000000000`);
  await expect(
    page.getByRole("heading", { name: label("en", `${C}.lead.notFoundTitle`) }),
  ).toBeVisible({ timeout: BOUND });
});

for (const locale of ["en", "ar"] as const) {
  test(`CJ-11 (${locale}): the inbox panel names the customer, creates a lead and moves it`, async ({
    page,
    context,
  }) => {
    const line = await connectedOwner("panel");
    const sender = freshCustomer();
    await inbound(line, sender, text("Hello"), "Nour Test");
    const conversation = await conversationOf(line.owner, sender);
    await signIn(page, context, locale, line.owner.email);
    // Assign it through the API so the header says who owns it (FEF-33's sentence).
    await page.goto(`${WEB}/inbox/${conversation.id}`);
    const panel = page.locator("[data-panel-contact]");
    await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible({ timeout: BOUND });
    // From 1280 px the panel starts shown; below that it is a drawer or a step behind its control.
    // The fact first: one of the two is on screen - never a guess from one instant.
    const show = page.locator("[data-show-panel]").first();
    await expect(panel.first().or(show).first()).toBeVisible({ timeout: BOUND });
    if (!(await panel.first().isVisible())) await show.click();
    await expect(panel.first()).toBeVisible({ timeout: BOUND });

    // No lead yet: create one from the panel, linked to the customer and the conversation.
    const creating = page.waitForRequest(
      (request) =>
        request.method() === "POST" && /\/api\/v1\/leads$/.test(new URL(request.url()).pathname),
    );
    const created = answerOf(page, "POST", /\/api\/v1\/leads$/);
    await page.locator("[data-create-lead]").first().click();
    await page
      .locator("[data-lead-create]")
      .getByRole("button", { name: label(locale, `${C}.create.submit`) })
      .click();
    const body = (await creating).postDataJSON() as Record<string, unknown>;
    expect(body["conversation_id"]).toBe(conversation.id);
    expect(body["name"]).toBe("Nour Test");
    expect((await created).status()).toBe(201);
    await expect(page.locator("[data-panel-lead-actions]").first()).toBeVisible({
      timeout: BOUND,
    });

    // A status move from the panel carries the status the panel shows.
    const moving = page.waitForRequest(
      (request) => request.method() === "POST" && request.url().endsWith("/status"),
    );
    await page.locator("[data-panel-lead-actions] [data-status-trigger]").first().click();
    await page
      .getByRole("menuitem", { name: label(locale, `${C}.status.moveTo.contacted`) })
      .click();
    expect((await moving).postDataJSON()).toEqual({
      status: "contacted",
      expected_status: "new",
    });
    await expect(
      page.locator('[data-panel-lead-actions] [data-lead-status="contacted"]').first(),
    ).toBeVisible({ timeout: BOUND });

    // The name from the panel: the API's conversation follows.
    const renamed = answerOf(page, "PATCH", /\/api\/v1\/contacts\/[^/]+$/);
    await panel
      .first()
      .getByRole("button", { name: label(locale, `${C}.contact.giveName`) })
      .click();
    await panel.first().getByRole("textbox").fill("Nour Panel Test");
    await panel.first().getByRole("textbox").press("Enter");
    expect((await renamed).status()).toBe(200);
    await expect
      .poll(
        async () =>
          (
            await read<{ participant: { display_name: string | null } }>(
              line.owner,
              `/api/v1/conversations/${conversation.id}`,
            )
          ).participant.display_name,
        { timeout: BOUND },
      )
      .toBe("Nour Panel Test");
    if (locale === "en") await axe(page, "inbox panel with a lead");
    await expect(page.locator("[data-open-customer]").first()).toHaveAttribute(
      "href",
      /\/contacts\/[0-9a-f-]{36}$/,
    );
    await expect(page.locator("[data-open-lead]").first()).toHaveAttribute(
      "href",
      /\/leads\/[0-9a-f-]{36}$/,
    );
  });
}

test("CJ-10: a withdrawn member's reload and a suspended sign-in land on a clear state", async ({
  page,
  context,
  browser,
}) => {
  // Withdrawn: the reload names no workspace, so the person lands where they still belong - here
  // nowhere, so the page offers to create a workspace. Never "Connection lost", never an error.
  const owner = await verifiedOwner("sess");
  const member = await memberOf(owner);
  await signIn(page, context, "en", member.email);
  await page.goto(`${WEB}/contacts`);
  await expect(page.locator('[data-screen="contacts"]')).toBeVisible({ timeout: BOUND });
  await revoke(owner, member);
  await page.reload();
  await page.waitForURL(/\/workspaces\/new|\/$/, { timeout: BOUND * 2 });
  await expect(page.getByText(label("en", "auth.connectionLostTitle"))).toHaveCount(0);
  await expect(page.locator('[data-screen="contacts"]')).toHaveCount(0);

  // Suspended: signing in when the only workspace is suspended shows GT-03's state.
  const other = await verifiedOwner("susp");
  await suspend(other.slug);
  const second = await actorContext(browser);
  const secondPage = await second.newPage();
  await useAddress(second);
  await useLocale(second, "en");
  await logInThroughTheApp(secondPage, "en", other.email);
  await expect(secondPage.locator('[data-gate="suspended"]')).toBeVisible({
    timeout: SUSPEND_BOUND_MS,
  });
  await expect(secondPage.getByRole("heading", { level: 1 })).toHaveText(
    label("en", "app.gates.suspended.title"),
  );
  await axe(secondPage, "suspended after sign-in");
  await second.close();
});
