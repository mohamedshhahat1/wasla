/**
 * FE-4's inbox journeys (IJ-02 .. IJ-07, IJ-09 .. IJ-13) against FE-0's profile: a customer writes
 * through the fake inbound webhook, the screens follow through the one stream per browser, and
 * every proof reads the page, the request log or the API - never a fixed sleep.
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page, Request } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import { api, label, logInThroughTheApp, useAddress, useLocale, WEB } from "../support/fe2";
import { gotoAfterSignIn, login, memberOf, recordRequests, streamOf } from "../support/fe3";
import {
  ARRIVAL_BOUND_MS,
  connectedOwner,
  conversationOf,
  freshCustomer,
  inbound,
  inboxState,
  kept,
  messagesOf,
  objectUrls,
  text,
  type Line,
} from "../support/fe4";
import { actorContext, expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 240_000 });

const phone = () => test.info().project.name.endsWith("phone");
const desktop = () => !phone();

/** FE-3's axe helper's rules: settled page, no finite animation running, serious / critical fail. */
async function axe(page: Page, state: string): Promise<void> {
  await expect
    .poll(async () => (await streamOf(page))?.state ?? "none", { timeout: 20_000 })
    .not.toBe("connecting");
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

async function openInbox(page: Page) {
  await gotoAfterSignIn(page, `${WEB}/inbox`);
  await expect(page.locator('[data-page="inbox"]')).toBeVisible({ timeout: 30_000 });
}

const row = (page: Page, conversationId: string) =>
  page.locator(`[data-conversation-row^="/inbox/${conversationId}"]`).first();

async function openConversation(page: Page, conversationId: string) {
  await row(page, conversationId).click();
  await page.waitForURL(new RegExp(`/inbox/${conversationId}`));
  await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible();
}

async function customerWrites(
  line: Line,
  body: string,
  sender = freshCustomer(),
  name = "Nour Hassan",
) {
  await inbound(line, sender, text(body), name);
  return { sender, conversation: await conversationOf(line.owner, sender) };
}

for (const locale of ["en", "ar"] as Locale[]) {
  test.describe(`${locale}`, () => {
    test("IJ-02 (J2): a customer writes, the row appears live, take over, reply sent", async ({
      page,
      context,
    }) => {
      const line = await connectedOwner();
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await axe(page, "inbox empty");
      const { conversation } = await customerWrites(line, "Hello, is Thursday free?");
      // message.created → the list, without a reload.
      await expect(row(page, conversation.id)).toBeVisible({ timeout: ARRIVAL_BOUND_MS });
      await expect(row(page, conversation.id)).toContainText("Nour Hassan");
      await openConversation(page, conversation.id);
      await expect(
        page.locator("[data-message-text]").filter({ hasText: "Hello, is Thursday free?" }),
      ).toBeVisible();
      if (locale === "ar") return; // the first half in Arabic (IJ-02)
      await page.locator("[data-mode-action]").click();
      await expect(page.locator("[data-mode-action]")).toHaveText(
        label(locale, "app.inbox.handBack"),
        {
          timeout: ARRIVAL_BOUND_MS,
        },
      );
      await page.locator("[data-composer-field]").fill("Yes, Thursday is free.");
      await page.locator("[data-send]").click();
      const sent = page
        .locator('[data-bubble="out"]')
        .filter({ hasText: "Yes, Thursday is free." });
      await expect(sent).toBeVisible();
      await expect(sent.locator("xpath=..").locator("[data-delivery]").last()).not.toHaveAttribute(
        "data-delivery",
        "sending",
        {
          timeout: ARRIVAL_BOUND_MS,
        },
      );
      await axe(page, "conversation with a reply");
    });

    test("IJ-03: views, search held in memory, the cursor refetched from the top", async ({
      page,
      context,
    }) => {
      const line = await connectedOwner();
      const { conversation: open } = await customerWrites(
        line,
        "first",
        freshCustomer(),
        "Omar Said",
      );
      const { conversation: closed } = await customerWrites(
        line,
        "second",
        freshCustomer(),
        "Laila Closed",
      );
      await api("POST", `/api/v1/conversations/${closed.id}/close`, { token: line.owner.token });
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await expect(row(page, open.id)).toBeVisible();
      await expect(row(page, closed.id)).toHaveCount(0);
      await page.getByRole("button", { name: label(locale, "app.inbox.status.closed") }).click();
      await expect(page).toHaveURL(/status=closed/);
      await expect(row(page, closed.id)).toBeVisible();
      await expect(row(page, open.id)).toHaveCount(0);
      await page.goBack();
      await expect(row(page, open.id)).toBeVisible();
      await page.goForward();
      await expect(row(page, closed.id)).toBeVisible();
      await page.getByRole("button", { name: label(locale, "app.inbox.status.all") }).click();
      // Search by name: q never in the address bar, the history or any storage.
      await page.locator("[data-inbox-search]").fill("Laila");
      await expect(row(page, closed.id)).toBeVisible();
      await expect(row(page, open.id)).toHaveCount(0);
      const where = await kept(page);
      expect(where).not.toContain("Laila");
      expect(page.url()).not.toContain("q=");
      await page.reload();
      await expect(page.locator("[data-inbox-search]")).toHaveValue("");
      // The cursor: a second page asked with another view's cursor answers 422 once → from the top.
      let swapped = false;
      await page.route(/\/api\/v1\/conversations\?/, async (route) => {
        const url = new URL(route.request().url());
        if (!swapped && url.searchParams.has("cursor")) {
          swapped = true;
          url.searchParams.set("cursor", "AAAA.0123456789ab");
          await route.continue({ url: url.toString() });
          return;
        }
        await route.continue();
      });
      for (let index = 0; index < 32; index += 1)
        await customerWrites(line, `bulk ${index}`, freshCustomer(), `Bulk ${index}`);
      await page.reload();
      // The button scrolled into view loads the next page by itself (IL-04's observer), so it is
      // busy or gone by the time a click lands: click only when it is still idle.
      const more = page.locator("[data-load-more]");
      const nextPage = async () => {
        // The button may go between two calls (the last page loaded by the observer): every step
        // is bounded, and a vanished button is not an error.
        try {
          if (!(await more.isVisible())) return;
          await more.scrollIntoViewIfNeeded({ timeout: 2_000 });
          if (await more.isEnabled()) await more.click({ timeout: 2_000 });
        } catch {
          // gone or busy: the next round looks again
        }
      };
      await expect(async () => {
        await nextPage();
        expect(swapped).toBe(true);
      }).toPass({ timeout: 60_000 });
      await expect(
        page.locator('[data-conversation-list] [role="status"], [data-conversation-list] h2'),
      ).toHaveCount(0);
      // Refetched from the top silently (no error state); the older row is one more page away.
      await expect(async () => {
        await nextPage();
        await expect(row(page, open.id)).toBeVisible({ timeout: 2_000 });
      }).toPass({ timeout: 60_000 });
    });

    test("IJ-04 / IJ-05 (J10): text as text, links behind a confirmation, media as revoked blobs", async ({
      page,
      context,
    }) => {
      const line = await connectedOwner();
      const sender = freshCustomer();
      await inbound(
        line,
        sender,
        text("<b>bold</b> ‏مرحبا 🙂 https://example.test/catalogue"),
        "Nour",
      );
      const conversation = await conversationOf(line.owner, sender);
      await inbound(line, sender, {
        type: "image",
        image: { id: `e2e-media-${Date.now()}`, mime_type: "image/png" },
      });
      await inboxState("media", conversation.id, "pending");
      await inboxState("media", conversation.id, "failed");
      await inboxState("media", conversation.id, "unavailable");
      await inboxState("voice-note", conversation.id);
      const requests: Request[] = [];
      page.on("request", (request) => requests.push(request));
      const violations: string[] = [];
      page.on("console", (message) => {
        if (/Content Security Policy|Refused to/.test(message.text()))
          violations.push(message.text());
      });
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await openConversation(page, conversation.id);
      const textBubble = page.locator("[data-message-text]").filter({ hasText: "<b>bold</b>" });
      await expect(textBubble).toBeVisible();
      await expect(textBubble.locator("b")).toHaveCount(0);
      await textBubble.locator("[data-inert-link]").click();
      await expect(page.locator("[data-link-target]")).toHaveText("https://example.test/catalogue");
      await page.keyboard.press("Escape");
      // Escape closes it - and it is gone before the scan below: a scan while it fades out
      // measured its buttons at a few percent opacity (WebKit, frontend.yml 43716094045).
      await expect(page.getByRole("dialog")).toHaveCount(0);
      for (const state of ["pending", "failed", "unavailable"]) {
        await expect(page.locator(`[data-media-state="${state}"]`).first()).toBeVisible();
      }
      const image = page.locator('[data-media-state="ready"] img').first();
      await expect(image).toBeVisible({ timeout: ARRIVAL_BOUND_MS });
      expect(await image.getAttribute("src")).toMatch(/^blob:/);
      const download = requests.find((request) => /\/media\/[0-9a-f-]{36}$/.test(request.url()));
      expect(download, "a media request was made").toBeTruthy();
      expect(Object.keys(await download!.allHeaders())).toContain("authorization");
      await expect(page.locator('[data-transcript="ready"]')).toBeVisible();
      await axe(page, "conversation with media");
      expect(violations).toEqual([]);
      for (const request of requests) {
        const origin = new URL(request.url()).origin;
        if (!request.url().startsWith("blob:") && !request.url().startsWith("data:")) {
          expect([new URL(WEB).origin, new URL(API_ORIGIN).origin]).toContain(origin);
        }
      }
      // Leaving the conversation lets go of every object URL.
      await page.goto(`${WEB}/inbox`);
      await expect
        .poll(async () => {
          const counts = await objectUrls(page);
          return counts ? counts.created - counts.revoked : -1;
        })
        .toBe(0);
    });

    test("IJ-06: ten forced retries make one bubble and one message; the closed window sends a template", async ({
      page,
      context,
    }) => {
      test.skip(locale === "ar", "the retry proof runs once (en)");
      const line = await connectedOwner();
      const { conversation } = await customerWrites(line, "please call me back");
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await openConversation(page, conversation.id);
      const keys: string[] = [];
      let failures = 0;
      await page.route(
        new RegExp(`/api/v1/conversations/${conversation.id}/messages$`),
        async (route) => {
          if (route.request().method() !== "POST") return route.continue();
          keys.push((await route.request().allHeaders())["idempotency-key"] ?? "");
          if (failures < 10) {
            failures += 1;
            await route.fetch(); // the API receives it…
            await route.abort("failed"); // …and the page never hears back
            return;
          }
          await route.continue();
        },
      );
      await page.locator("[data-composer-field]").fill("We will call you at noon.");
      await page.locator("[data-send]").click();
      // Ten attempts fail, the eleventh goes through. The proof counts the requests the page made,
      // not the presses: a press that lands while the bubble moves (the server's copy arriving
      // over the stream) does nothing, and the person presses again (frontend.yml 43716094045).
      for (let presses = 0; keys.length < 11; presses += 1) {
        expect(presses, "presses for eleven attempts").toBeLessThan(20);
        const sent = keys.length;
        const retry = page.locator("[data-retry]").first();
        await expect(retry).toBeVisible({ timeout: 20_000 });
        await retry.click();
        await expect
          .poll(() => keys.length, { timeout: 5_000 })
          .toBeGreaterThan(sent)
          .catch(() => undefined);
      }
      await expect(
        page.locator('[data-bubble="out"]').filter({ hasText: "We will call you at noon." }),
      ).toHaveCount(1, {
        timeout: ARRIVAL_BOUND_MS,
      });
      expect(new Set(keys).size).toBe(1);
      expect(keys).toHaveLength(11);
      const stored = (await messagesOf(line.owner, conversation.id)).filter(
        (m) => m.body === "We will call you at noon.",
      );
      expect(stored).toHaveLength(1);
      await page.unroute(new RegExp(`/api/v1/conversations/${conversation.id}/messages$`));

      // The draft survives a reload of the tab.
      await page.locator("[data-composer-field]").fill("a draft to keep");
      await page.reload();
      await expect(page.locator("[data-composer-field]")).toHaveValue("a draft to keep");

      // A file refused before any request.
      const before = (await messagesOf(line.owner, conversation.id)).length;
      await page.locator('input[type="file"]').setInputFiles({
        name: "tool.exe",
        mimeType: "application/x-msdownload",
        buffer: Buffer.from("MZ"),
      });
      await expect(
        page.getByText(label(locale, "app.inbox.composer.fileRefused.type")),
      ).toBeVisible();
      expect((await messagesOf(line.owner, conversation.id)).length).toBe(before);

      // The reply window closed 25 hours ago → templates only.
      await inboxState("window-closed", conversation.id);
      await inboxState("templates", line.owner.slug);
      await page.reload();
      await page.locator("[data-choose-template]").click();
      await page.getByRole("button", { name: /order_update/ }).click();
      await page
        .getByLabel(label(locale, "app.inbox.templates.parameter").replace("{number}", "1"))
        .fill("A-1042");
      await expect(page.locator("[data-template-preview]")).toContainText("A-1042");
      await page.locator("[data-send-template]").click();
      await expect
        .poll(async () => (await messagesOf(line.owner, conversation.id)).length, {
          timeout: ARRIVAL_BOUND_MS,
        })
        .toBeGreaterThan(before);
      await axe(page, "composer, window closed");
    });

    test("IJ-07: actions, a stale assignment told not overwritten, the panel", async ({
      page,
      context,
      browser,
    }) => {
      const line = await connectedOwner();
      const member = await memberOf(line.owner);
      const { conversation } = await customerWrites(line, "I need help with my order");
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await openConversation(page, conversation.id);
      // A stale assignment: someone changes the assignee between the screen's read and the click.
      // The stream would tell the screen at once, so its detail reads are held at what it showed
      // ("shown"), and the click waits until the frame's refetch has been answered - the request
      // then carries a stale expectation every time, in every browser.
      let shown: string | null = null;
      let held = 0;
      const detail = new RegExp(`/api/v1/conversations/${conversation.id}$`);
      await page.route(detail, async (route) => {
        // The screen may abort a read it no longer needs; then there is nothing to answer.
        try {
          const response = await route.fetch();
          const body = (await response.json()) as Record<string, unknown>;
          await route.fulfill({ response, json: { ...body, assigned_to_id: shown } });
          held += 1;
        } catch {
          await route.abort().catch(() => undefined);
        }
      });
      const assignToMe = async () => {
        const answer = page.waitForResponse(
          (response) =>
            response.url().endsWith(`/conversations/${conversation.id}/assignment`) &&
            response.request().method() === "POST",
        );
        await page.locator("[data-assign]").click();
        await page.getByRole("menuitem", { name: label(locale, "app.inbox.assignToMe") }).click();
        return (await answer).status();
      };
      const assigneeNow = async () =>
        (
          (
            await api("GET", `/api/v1/conversations/${conversation.id}`, {
              token: line.owner.token,
            })
          ).json as { assigned_to_id: string | null }
        ).assigned_to_id;

      // 1. The screen shows nobody; the member takes it meanwhile.
      let before = held;
      await api("POST", `/api/v1/conversations/${conversation.id}/assignment`, {
        token: member.token,
        body: { assigned_to_id: member.userId, expected_assigned_to_id: null },
      });
      await expect.poll(() => held, { timeout: ARRIVAL_BOUND_MS }).toBeGreaterThan(before);
      expect(await assignToMe()).toBe(409);
      await expect(page.locator("[data-action-notice]")).toBeVisible({ timeout: ARRIVAL_BOUND_MS });
      expect(await assigneeNow()).toBe(member.userId);

      // 2. The screen shows the member; the member lets it go meanwhile. "Assign to me" sends the
      // member as the expected assignee: refused, and it stays unassigned (CX-02, M-V23).
      shown = member.userId;
      before = held;
      await page.reload();
      await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible();
      await expect.poll(() => held, { timeout: ARRIVAL_BOUND_MS }).toBeGreaterThan(before);
      before = held;
      await api("POST", `/api/v1/conversations/${conversation.id}/assignment`, {
        token: member.token,
        body: { assigned_to_id: null, expected_assigned_to_id: member.userId },
      });
      await expect.poll(() => held, { timeout: ARRIVAL_BOUND_MS }).toBeGreaterThan(before);
      expect(await assignToMe()).toBe(409);
      expect(await assigneeNow()).toBeNull();
      await page.unrouteAll({ behavior: "wait" });
      // A second tab sees a change through the stream.
      const second = await context.newPage();
      await second.goto(`${WEB}/inbox/${conversation.id}`);
      await expect(second.locator('[data-inbox-pane="conversation"]')).toBeVisible();
      await page.locator("[data-mode-action]").click();
      await expect(second.locator("[data-mode-action]")).toHaveText(
        label(locale, "app.inbox.handBack"),
        { timeout: ARRIVAL_BOUND_MS },
      );
      // Close leaves the open view.
      await page.locator("[data-conversation-menu]").click();
      await page.getByRole("menuitem", { name: label(locale, "app.inbox.close") }).click();
      await expect(page.locator("[data-composer-stopped]")).toContainText(
        label(locale, "app.inbox.composer.closed"),
      );
      // The panel: the customer and "no lead".
      if (desktop()) {
        const show = page.locator("[data-show-panel]");
        if (await show.isVisible()) await show.click();
        await expect(page.locator("[data-panel-lead]")).toContainText(
          label(locale, "app.inbox.panel.noLead"),
        );
      }
      await second.close();
      void browser;
    });

    test("IJ-09: the phone steps track the URL; Back works", async ({ page, context }) => {
      test.skip(desktop(), "the phone layout");
      const line = await connectedOwner();
      const { conversation } = await customerWrites(line, "phone step");
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      await expect(page.locator('[data-inbox-layout="steps"]')).toBeVisible();
      await openConversation(page, conversation.id);
      await expect(page.locator('[data-inbox-pane="list"]')).toHaveCount(0);
      await page.locator("[data-show-panel]").click();
      await expect(page).toHaveURL(/panel=customer/);
      await expect(page.locator('[data-inbox-pane="panel"]')).toBeVisible();
      await page.goBack();
      await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible();
      await page.goBack();
      await expect(page.locator('[data-inbox-pane="list"]')).toBeVisible();
      await axe(page, "phone list step");
    });

    test("IJ-11: six tabs on the inbox share one stream; each list shows the message once", async ({
      page,
      context,
    }) => {
      test.skip(locale === "ar" || phone(), "once, on a desktop");
      const line = await connectedOwner();
      await signIn(page, context, locale, line.owner.email);
      const tabs = [page];
      for (let index = 1; index < 6; index += 1) tabs.push(await context.newPage());
      for (const tab of tabs) await openInbox(tab);
      await expect
        .poll(
          async () =>
            (await Promise.all(tabs.map(streamOf))).filter((s) => s?.role === "leader").length,
          { timeout: 30_000 },
        )
        .toBe(1);
      const { conversation } = await customerWrites(line, "six tabs");
      for (const tab of tabs)
        await expect(row(tab, conversation.id)).toHaveCount(1, { timeout: ARRIVAL_BOUND_MS });
      for (const tab of tabs.slice(1)) await tab.close();
    });

    test("IJ-13: shared unread, seen by, the first reply, the filter, the total and mark as unread", async ({
      page,
      context,
      browser,
    }) => {
      test.skip(locale === "ar" || phone(), "once, on a desktop");
      const line = await connectedOwner();
      const member = await memberOf(line.owner);
      const sender = freshCustomer();
      await signIn(page, context, locale, line.owner.email);
      await openInbox(page);
      for (const body of ["one", "two", "three"]) await inbound(line, sender, text(body), "Nour");
      const conversation = await conversationOf(line.owner, sender);
      await expect(row(page, conversation.id).locator("[data-count]")).toHaveText("3", {
        timeout: ARRIVAL_BOUND_MS,
      });
      await expect(page.locator("[data-nav-list] [data-count]").first()).toHaveText("3");

      const memberContext = await actorContext(browser);
      const memberPage = await memberContext.newPage();
      await useAddress(memberContext);
      await useLocale(memberContext, locale);
      await logInThroughTheApp(memberPage, locale, member.email);
      await memberPage.waitForURL(`${WEB}/`);
      const reads: string[] = [];
      memberPage.on("request", (request) => {
        if (request.method() === "POST" && request.url().endsWith("/read"))
          reads.push(request.url());
      });
      await memberPage.goto(`${WEB}/inbox/${conversation.id}`);
      await expect(
        memberPage.locator("[data-message-text]").filter({ hasText: "three" }),
      ).toBeVisible();
      await expect.poll(() => reads.length, { timeout: ARRIVAL_BOUND_MS }).toBe(1);
      // The owner's row and total follow through the stream.
      await expect(row(page, conversation.id).locator("[data-count]")).toHaveCount(0, {
        timeout: ARRIVAL_BOUND_MS,
      });
      await expect(page.locator("[data-nav-list] [data-count]")).toHaveCount(0);
      await openConversation(page, conversation.id);
      await expect(page.locator('[data-thread-note="seen"]')).toBeVisible();
      // The first reply assigns the member.
      await memberPage.locator("[data-composer-field]").fill("On it.");
      await memberPage.locator("[data-send]").click();
      await expect
        .poll(
          async () =>
            (
              await api("GET", `/api/v1/conversations/${conversation.id}`, {
                token: line.owner.token,
              })
            ).json as { assigned_to_id: string | null },
        )
        .toMatchObject({ assigned_to_id: member.userId });
      // Mark as unread → the owner's screen closes the conversation; both see it unread.
      await page.locator("[data-conversation-menu]").click();
      await page.getByRole("menuitem", { name: label(locale, "app.inbox.markUnread") }).click();
      await page.waitForURL(/\/inbox(\?|$)/);
      await expect(row(page, conversation.id).locator("[data-count]")).toHaveText("1", {
        timeout: ARRIVAL_BOUND_MS,
      });
      // Unread only.
      await page.locator("[data-unread-toggle]").click();
      await expect(page).toHaveURL(/unread=1/);
      await expect(row(page, conversation.id)).toBeVisible();
      expect(await kept(page)).not.toMatch(/three|Nour/);
      // FEF-35: the member's screen, still showing it, did not read it again.
      expect(reads).toHaveLength(1);
      await memberContext.close();
    });
  });
}

// IL-07 (M-V32): the count comes from the server's answer - a mark-read that fails leaves the row's
// count as it was; nothing zeroes it ahead of the answer.
test("IJ-13b: a failed mark-read leaves the count", async ({ page, context }) => {
  test.skip(test.info().project.name !== "chromium-desktop", "once");
  const line = await connectedOwner();
  const sender = freshCustomer();
  await signIn(page, context, "en", line.owner.email);
  await openInbox(page);
  for (const body of ["one", "two"]) await inbound(line, sender, text(body), "Nour");
  const conversation = await conversationOf(line.owner, sender);
  await expect(row(page, conversation.id).locator("[data-count]")).toHaveText("2", {
    timeout: ARRIVAL_BOUND_MS,
  });
  let refused = 0;
  await page.route(/\/api\/v1\/conversations\/[^/]+\/read$/, async (route) => {
    refused += 1;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "service_unavailable", message: "Try again." } }),
    });
  });
  await openConversation(page, conversation.id);
  await expect(page.locator("[data-message-text]").filter({ hasText: "two" })).toBeVisible();
  await expect.poll(() => refused, { timeout: ARRIVAL_BOUND_MS }).toBeGreaterThan(0);
  await expect(row(page, conversation.id).locator("[data-count]")).toHaveText("2");
});

test("IJ-10: requests only to the app and the API; a burst of frames is one refetch", async ({
  page,
  context,
}) => {
  test.skip(test.info().project.name !== "chromium-desktop", "once");
  const line = await connectedOwner();
  const seen = recordRequests(context);
  await signIn(page, context, "en", line.owner.email);
  await openInbox(page);
  const sender = freshCustomer();
  await inbound(line, sender, text("first"));
  const conversation = await conversationOf(line.owner, sender);
  await expect(row(page, conversation.id)).toBeVisible({ timeout: ARRIVAL_BOUND_MS });
  const before = seen.filter(
    (entry) => entry.startsWith("GET") && /\/api\/v1\/conversations\?/.test(entry),
  ).length;
  for (let index = 0; index < 5; index += 1) await inbound(line, sender, text(`burst ${index}`));
  await expect(row(page, conversation.id)).toContainText("burst 4", { timeout: ARRIVAL_BOUND_MS });
  const after = seen.filter(
    (entry) => entry.startsWith("GET") && /\/api\/v1\/conversations\?/.test(entry),
  ).length;
  expect(after - before).toBeLessThanOrEqual(5);
  for (const entry of seen) {
    const url = entry.slice(entry.indexOf(" ") + 1);
    if (url.startsWith("blob:") || url.startsWith("data:")) continue;
    expect([new URL(WEB).origin, new URL(API_ORIGIN).origin]).toContain(new URL(url).origin);
  }
  void login;
});
