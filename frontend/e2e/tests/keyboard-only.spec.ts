/**
 * KY-02: one keyboard-only journey (FE-5's CJ-12 half that was not written) over FE-4's inbox,
 * FE-5's customers and leads and FE-6's AI and knowledge screens. No pointer at all: the first
 * test reads this file and fails if a mouse call appears in it. Each step waits on a fact.
 */
import { readFileSync } from "node:fs";

import type { Page } from "@playwright/test";

import { label, logInThroughTheApp, useAddress, useLocale, WEB } from "../support/fe2";
import { connectedOwner, freshCustomer, inbound, text } from "../support/fe4";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 300_000 });

const KEYS_ONLY =
  /\.(click|dblclick|hover|tap|dragTo|check|uncheck|selectOption|setChecked)\(|page\.mouse|\.mouse\./;

test("KY-02: this journey uses no pointer API", () => {
  const source = readFileSync(__filename, "utf-8")
    .split("\n")
    .filter((line) => !line.includes("KEYS_ONLY"))
    .join("\n");
  expect(source.match(KEYS_ONLY)).toBeNull();
});

type Target =
  | { name: string }
  | { action: string }
  | { role: string }
  | { inside: string }
  | { rowLinkPrefix: string };

/** Tab (or Shift+Tab) until the focused element is the target, bounded; the fact is focus. */
async function tabTo(page: Page, target: Target, reverse = false): Promise<void> {
  for (let stop = 0; stop < 200; stop += 1) {
    const hit = await page.evaluate((wanted) => {
      const el = document.activeElement;
      if (!el) return false;
      if ("name" in wanted) return (el.textContent ?? "").trim() === wanted.name;
      if ("action" in wanted) return el.getAttribute("data-action") === wanted.action;
      if ("role" in wanted) return el.getAttribute("role") === wanted.role;
      if ("inside" in wanted) return el.closest(wanted.inside) !== null;
      return (el.getAttribute("data-row-link") ?? "").startsWith(wanted.rowLinkPrefix);
    }, target);
    if (hit) return;
    await page.keyboard.press(reverse ? "Shift+Tab" : "Tab");
  }
  throw new Error(`never reached by Tab: ${JSON.stringify(target)}`);
}

for (const locale of ["en", "ar"] as const) {
  test(`KY-02 (${locale}): inbox → customer → lead → agent → knowledge, keyboard only`, async ({
    page,
    context,
  }) => {
    const line = await connectedOwner(`kb${locale}`);
    await inbound(line, freshCustomer(), text("Hello"));
    await useAddress(context);
    await useLocale(context, locale);
    await logInThroughTheApp(page, locale, line.owner.email);
    await page.waitForURL(`${WEB}/`);
    await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });

    // Home → the warning's link → the agents list (Enter on a link).
    await tabTo(page, { name: label(locale, "app.homeWarnings.noAgentAction") });
    await page.keyboard.press("Enter");
    await page.waitForURL(`${WEB}/ai/agents`);

    // New agent: the dialog keeps focus; Escape returns it to the opener.
    const create = label(locale, "app.ai.emptyAction");
    await tabTo(page, { name: create });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => (document.activeElement?.textContent ?? "").trim()))
      .toBe(create);
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-field="agent-name"]')).toBeFocused();
    await page.keyboard.type("Keyboard agent");
    await page.keyboard.press("Tab");
    await page.keyboard.type("You answer questions about the test company.");
    const created = page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/api\/v1\/agents$/.test(r.url()),
    );
    await page.evaluate(() => document.querySelector<HTMLFormElement>("form[id]")?.requestSubmit());
    expect((await created).status()).toBe(201);
    await page.waitForURL(/\/ai\/agents\/[0-9a-f-]{36}$/);

    // Activate, then make default - Enter on each; the status says so.
    await tabTo(page, { action: "activate" });
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-action="makeDefault"]')).toBeVisible();
    await tabTo(page, { action: "makeDefault" }, true);
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-status-sentence="answering"]')).toBeVisible();

    // A tool's switch: Space.
    await tabTo(page, { role: "switch" }, true);
    const before = await page.evaluate(() => document.activeElement?.getAttribute("aria-checked"));
    const toggled = page.waitForResponse(
      (r) => /\/tools(\/|$)/.test(new URL(r.url()).pathname) && r.request().method() !== "GET",
    );
    await page.keyboard.press("Space");
    await toggled;
    await expect
      .poll(() => page.locator('[role="switch"]').first().getAttribute("aria-checked"))
      .not.toBe(before);

    // Knowledge: a new base, "Add text", then the row opens its panel; Escape closes it.
    await page.goto(`${WEB}/knowledge`);
    await tabTo(page, { action: "new-base" });
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-field="base-name"]')).toBeFocused();
    await page.keyboard.type("Keys");
    await page.keyboard.press("Enter");
    await page.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/);
    await tabTo(page, { action: "add-text" });
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-field="text-title"]')).toBeFocused();
    await page.keyboard.type("Opening hours");
    await page.keyboard.press("Tab");
    await page.keyboard.type("Open from 9 to 5. (e2e)");
    const added = page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/documents$/.test(new URL(r.url()).pathname),
    );
    await page.evaluate(() =>
      document.querySelector<HTMLFormElement>("[role=dialog] form")?.requestSubmit(),
    );
    expect((await added).status()).toBe(202);
    await tabTo(page, { inside: "[data-document-row]" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/doc=/);
    await page.keyboard.press("Escape");
    await expect(page).not.toHaveURL(/doc=/);

    // The inbox → a conversation → the customer's page → the lead's status, keys only.
    await page.goto(`${WEB}/inbox`);
    await tabTo(page, { inside: "[data-conversation-row]" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/inbox\/[0-9a-f-]{36}/);
    await page.goto(`${WEB}/contacts`);
    await tabTo(page, { rowLinkPrefix: "/contacts/" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/contacts\/[0-9a-f-]{36}$/);
    await page.keyboard.press("Alt+ArrowLeft");
    await expect(page).toHaveURL(`${WEB}/contacts`);
  });
}
