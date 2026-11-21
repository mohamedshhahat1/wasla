/**
 * KY-03: focus not obscured (WCAG 2.2 2.4.11) over FE-4's, FE-5's and FE-6's screens (FE-7's
 * team and settings screens, and - FE-8 leftovers, KY8-03 - every billing screen state), at 1280
 * and 390, in en and ar. Tab from the top until focus comes round (bounded at 200 stops); at each stop
 * the focused element's box is compared with every fixed or sticky element drawn above it. Fully
 * covered fails; covered past half its area is reported (2.4.12, AAA) and does not fail.
 */
import type { Page } from "@playwright/test";

import { logInThroughTheApp, useAddress, useLocale, WEB } from "../support/fe2";
import { memberOf } from "../support/fe3";
import { connectedOwner, conversationOf, freshCustomer, inbound, text } from "../support/fe4";
import { answeringAgent, baseWithDocument, settled } from "../support/fe6";
import { axeScan, checkoutAnswer, returnSucceeded } from "../support/fe8";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 600_000 });

interface Stop {
  stop: number;
  element: string;
  covered: number;
  by: string | null;
}

/** The share of the focused element hidden by fixed / sticky elements drawn above it, 0..1. */
export async function coverOfFocus(
  page: Page,
): Promise<{ element: string; covered: number; by: string | null } | null> {
  return page.evaluate(() => {
    const focused = document.activeElement as HTMLElement | null;
    if (!focused || focused === document.body) return null;
    const box = focused.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) return null;
    const area = box.width * box.height;
    const covers: Array<{ name: string; rect: DOMRect }> = [];
    for (const el of Array.from(document.body.querySelectorAll<HTMLElement>("*"))) {
      if (el === focused || el.contains(focused) || focused.contains(el)) continue;
      const style = getComputedStyle(el);
      if (style.position !== "fixed" && style.position !== "sticky") continue;
      if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0)
        continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      // Above the focused element in paint order: the cover's own stacking is enough to tell.
      const hit = document.elementFromPoint(
        Math.min(Math.max(box.left + box.width / 2, rect.left + 1), rect.right - 1),
        Math.min(Math.max(box.top + box.height / 2, rect.top + 1), rect.bottom - 1),
      );
      if (!hit || !el.contains(hit)) continue;
      covers.push({
        name: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}[${el.className.toString().slice(0, 40)}]`,
        rect,
      });
    }
    let worst = 0;
    let by: string | null = null;
    for (const cover of covers) {
      const width = Math.max(
        0,
        Math.min(box.right, cover.rect.right) - Math.max(box.left, cover.rect.left),
      );
      const height = Math.max(
        0,
        Math.min(box.bottom, cover.rect.bottom) - Math.max(box.top, cover.rect.top),
      );
      const share = (width * height) / area;
      if (share > worst) {
        worst = share;
        by = cover.name;
      }
    }
    const name = `${focused.tagName.toLowerCase()} ${(focused.getAttribute("aria-label") ?? focused.textContent ?? "").trim().slice(0, 40)}`;
    return { element: name, covered: worst, by };
  });
}

async function sweep(page: Page, screen: string): Promise<Stop[]> {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const stops: Stop[] = [];
  let first: string | null = null;
  for (let stop = 0; stop < 200; stop += 1) {
    await page.keyboard.press("Tab");
    const id = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el) return "";
      if (!el.dataset["kyStop"]) el.dataset["kyStop"] = String(Math.random());
      return el.dataset["kyStop"];
    });
    if (first === null) first = id;
    else if (id === first) break;
    const cover = await coverOfFocus(page);
    if (cover) stops.push({ stop, ...cover });
  }
  test.info().annotations.push({
    type: `KY-03 ${screen}`,
    description: JSON.stringify({
      stops: stops.length,
      fullyCovered: stops.filter((s) => s.covered >= 0.999).length,
      partly: stops
        .filter((s) => s.covered > 0.5 && s.covered < 0.999)
        .map((s) => [s.element, Math.round(s.covered * 100), s.by]),
    }),
  });
  return stops;
}

for (const [width, height] of [
  [1280, 800],
  [390, 844],
] as const) {
  for (const locale of ["en", "ar"] as const) {
    test(`KY-03 (${locale}, ${width}): no focused element is fully hidden`, async ({
      page,
      context,
    }) => {
      // Each width is swept once: in the phone project at 390, in the desktop project at 1280.
      test.skip(
        (width === 390) !== /phone/.test(test.info().project.name),
        "the other project sweeps this width",
      );
      await page.setViewportSize({ width, height });
      const line = await connectedOwner(`ky${locale}${width}`);
      const sender = freshCustomer();
      await inbound(line, sender, text("Hello"));
      const conversation = await conversationOf(line.owner, sender);
      const agentId = await answeringAgent(line.owner);
      const { baseId, documentId } = await baseWithDocument(line.owner, "Facts");
      await settled(line.owner, documentId);
      await useAddress(context);
      await useLocale(context, locale);
      await logInThroughTheApp(page, locale, line.owner.email);
      await page.waitForURL(`${WEB}/`);

      const screens: Array<[string, string, string]> = [
        ["inbox", "/inbox", "[data-conversation-row], [data-screen]"],
        ["conversation", `/inbox/${conversation.id}`, "textarea"],
        ["contacts", "/contacts", '[data-screen="contacts"]'],
        ["contact", `/contacts/${conversation.contact_id}`, "h1"],
        ["leads", "/leads", "h1"],
        ["agents", "/ai/agents", '[data-screen="agents"]'],
        ["agent", `/ai/agents/${agentId}`, '[data-screen="agent"]'],
        ["knowledge", "/knowledge", '[data-screen="knowledge"]'],
        [
          "knowledge-base",
          `/knowledge/${baseId}?doc=${documentId}`,
          '[data-screen="knowledge-base"]',
        ],
        // FE-7 (KY7-03): the team, settings and account screens.
        ["team", "/team", "[data-members-list]"],
        ["invitations", "/team/invitations", '[data-screen="invitations"]'],
        ["workspace-settings", "/settings/workspace", "[data-workspace-form]"],
        ["activity", "/settings/activity", "[data-settings]"],
        ["close-workspace", "/settings/workspace/close", '[data-field="close-confirmation"]'],
        ["account", "/settings/account", '[data-action="close-account"]'],
      ];
      const failures: string[] = [];
      for (const [name, path, ready] of screens) {
        // Every full load refreshes the session, and /auth/refresh allows ten a minute per client
        // address (AuthRateLimit): FE-7's six screens are swept from a second test address.
        if (name === "team") await useAddress(context);
        await page.goto(`${WEB}${path}`);
        await expect(page.locator(ready).first()).toBeVisible({ timeout: 30_000 });
        if (name === "agent") {
          // Dirty, so the sticky Save bar is drawn: the case that can hide a field.
          await page.locator('[data-field="system_prompt"]').fill("x".repeat(3000));
        }
        if (name === "workspace-settings") {
          // Dirty, so the save bar is drawn over the end of the form (KY7-03).
          await page.locator('[data-field="workspace-name"]').fill("Renamed for the sweep");
        }
        const stops = await sweep(page, `${name} ${locale} ${width}`);
        for (const stop of stops.filter((s) => s.covered >= 0.999)) {
          failures.push(`${name}: stop ${stop.stop} ${stop.element} hidden by ${stop.by}`);
        }
      }
      expect(failures).toEqual([]);
    });
  }
}

// FE-8 leftovers (KY8-03 / KYL-03): every billing screen state, at 1280 and 390, en and ar. The
// owner pays for pro on the fake page first (so invoices, a paid invoice's attempts and a cancel
// dialog exist); the dialogs are swept as themselves (focus stays inside them until Escape).
for (const [width, height] of [
  [1280, 800],
  [390, 844],
] as const) {
  for (const locale of ["en", "ar"] as const) {
    test(`KY8-03 (${locale}, ${width}): no focused element is fully hidden on Billing`, async ({
      page,
      context,
    }) => {
      test.skip(
        (width === 390) !== /phone/.test(test.info().project.name),
        "the other project sweeps this width",
      );
      await page.setViewportSize({ width, height });
      const line = await connectedOwner(`ky8${locale}${width}`);
      await memberOf(line.owner);
      await useAddress(context);
      await useLocale(context, locale);
      await logInThroughTheApp(page, locale, line.owner.email);
      await page.waitForURL(`${WEB}/`);

      // Pay for pro on the fake page: the paid invoice, its attempt and the subscription exist.
      await page.goto(`${WEB}/billing`);
      const upgrade = page.locator('[data-plan-price="pro:monthly"]');
      await expect(upgrade).toBeVisible({ timeout: 30_000 });
      const started = checkoutAnswer(page);
      await upgrade.press("Enter");
      expect((await started).status()).toBe(201);
      await returnSucceeded(page);

      const failures: string[] = [];
      const sweepOn = async (name: string) => {
        await axeScan(page, `${name} ${locale} ${width}`);
        const stops = await sweep(page, `${name} ${locale} ${width}`);
        for (const stop of stops.filter((s) => s.covered >= 0.999)) {
          failures.push(`${name}: stop ${stop.stop} ${stop.element} hidden by ${stop.by}`);
        }
      };
      await sweepOn("billing-return-succeeded");

      // Each full load refreshes the session (ten a minute per address): a fresh address here.
      await useAddress(context);
      const screens: Array<[string, string, string]> = [
        ["billing", "/billing", "[data-plans]"],
        ["billing-invoices", "/billing/invoices", '[data-action="open-invoice"]'],
        ["billing-methods", "/billing/payment-methods", '[data-screen="billing-methods"]'],
        ["billing-usage", "/billing/usage", "[data-bar-series], [data-screen='billing-usage']"],
        [
          "billing-capacity",
          "/billing/capacity",
          "[data-topups], [data-screen='billing-capacity']",
        ],
        ["team", "/team", "[data-members-list]"],
        ["invitations", "/team/invitations", '[data-screen="invitations"]'],
      ];
      for (const [name, path, ready] of screens) {
        await page.goto(`${WEB}${path}`);
        await expect(page.locator(ready).first()).toBeVisible({ timeout: 30_000 });
        await expect(page.locator('[role="status"]', { hasText: /…|\.\.\./ })).toHaveCount(0);
        await sweepOn(name);
        if (name === "billing") {
          await page.locator('[data-action="cancel-subscription"]').press("Enter");
          await expect(page.getByRole("dialog")).toBeVisible();
          await sweepOn("billing-cancel-dialog");
          await page.keyboard.press("Escape");
          await expect(page.getByRole("dialog")).toHaveCount(0);
        }
        if (name === "billing-invoices") {
          await page.locator('[data-action="open-invoice"]').first().press("Enter");
          await expect(page.locator("[data-invoice-page]")).toBeVisible({ timeout: 30_000 });
          await sweepOn("billing-invoice");
          await page.locator('[data-action="ask-refund"]').press("Enter");
          await expect(page.getByRole("dialog")).toBeVisible();
          await sweepOn("billing-refund-dialog");
          await page.keyboard.press("Escape");
          await expect(page.getByRole("dialog")).toHaveCount(0);
        }
        if (name === "team") {
          await page.locator('[data-action="change-role"]').first().press("Enter");
          await expect(page.getByRole("dialog")).toBeVisible();
          await sweepOn("team-change-role-dialog");
          await page.keyboard.press("Escape");
          await expect(page.getByRole("dialog")).toHaveCount(0);
        }
      }
      expect(failures).toEqual([]);
    });
  }
}

test("KY-03 control (M-U42): a planted cover over a focus stop is found", async ({ page }) => {
  await page.setContent(
    '<button id="b" style="position:absolute;top:10px;left:10px">x</button><div style="position:fixed;top:0;left:0;width:200px;height:100px;background:#fff"></div>',
  );
  await page.keyboard.press("Tab");
  const cover = await coverOfFocus(page);
  expect(cover?.covered).toBeGreaterThanOrEqual(0.999);
});
