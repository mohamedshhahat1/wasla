/**
 * FE-8 leftovers' journey helpers (KYL-04, VQL-02): the billing states the API cannot reach in a
 * few calls (tests/e2e_stack/billing_states.py through `docker exec` on FE-0's API container, as
 * fe3's gateState), a paid pro plan through the fake Paymob page, the window.print stub, and the
 * axe scan of one screen state (FGL-6: after the waits FE-3 / BE1 / FE-5 put before a scan).
 * Every wait is on a fact - an answer, a state the page reports - never on a fixed moment.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import AxeBuilder from "@axe-core/playwright";
import { expect, type BrowserContext, type Page } from "@playwright/test";

import { API_ORIGIN } from "./env";
import { WEB } from "./fe2";
import { API_CONTAINER } from "./fe3";

const exec = promisify(execFile);
const BOUND = 30_000;

/** The test-only billing seed (EC8-04): a saved card, or a paid checkout refunded in full. */
export async function billingState(
  command: "saved-card" | "refunded",
  slug: string,
): Promise<Record<string, string>> {
  const { stdout } = await exec("docker", [
    "exec",
    API_CONTAINER,
    "python",
    "-m",
    "tests.e2e_stack.billing_states",
    command,
    slug,
  ]);
  return JSON.parse(stdout.trim().split("\n").pop()!) as Record<string, string>;
}

/**
 * KYL-02: "Print" calls window.print; the stub records the call and the browser's print dialog
 * never opens. Installed before the app loads; read with `printCalls`.
 */
export async function stubPrint(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    const calls = { count: 0 };
    (window as unknown as { __fe8lPrint: { count: number } }).__fe8lPrint = calls;
    window.print = () => {
      calls.count += 1;
    };
  });
}

export async function printCalls(page: Page): Promise<number> {
  return page.evaluate(
    () => (window as unknown as { __fe8lPrint?: { count: number } }).__fe8lPrint?.count ?? 0,
  );
}

/** Waits for the return page to settle on succeeded - a state the page reports. */
export async function returnSucceeded(page: Page): Promise<void> {
  await page.waitForURL(`${WEB}/billing/checkout/return**`, { timeout: BOUND });
  await expect(page.locator('[data-return-state="succeeded"]')).toBeVisible({ timeout: 60_000 });
}

/** The checkout POST's answer, started before the press that sends it. */
export function checkoutAnswer(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.url().startsWith(`${API_ORIGIN}/api/v1/billing/checkout`) &&
      response.request().method() === "POST",
    { timeout: BOUND },
  );
}

/** Sends the fake page the journey's outcome (the API never sees it - EC8-04), as BJ's specs do. */
export async function payWith(context: BrowserContext, outcome: string): Promise<void> {
  await context.route("http://localhost:8026/paymob/**", async (route) => {
    const url = new URL(route.request().url());
    for (const [key, value] of new URLSearchParams(outcome)) url.searchParams.set(key, value);
    await route.continue({ url: url.toString() });
  });
}

/** axe on the state on screen: no closing dialog, no busy button, no finite animation running. */
export async function axeScan(page: Page, state: string): Promise<void> {
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
  expect(
    result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => `${state}: ${v.id} | ${v.nodes.map((n) => n.target.join(" ")).join(" || ")}`),
  ).toEqual([]);
}
