/**
 * FE-4's IJ-08 (the plan's "kill the API → banner → recovers with resync"): with the inbox open,
 * the API stops - the shell says "Live updates paused - retrying", the inbox stays readable and
 * polls at its interval (never while hidden); it starts - live again with a new ticket, the gap rule
 * reads the open view and conversation from the top, and a customer's message arrives without a
 * reload. Stopping the API would fail every journey beside it, so this file runs alone in the
 * "api-down" project (FE-3's 5f8fb31). A message cannot be injected while the API is down (the
 * webhook is the API): it is injected the moment the API answers again, before the new stream
 * opens, and must still arrive - through replay or the gap refetch.
 */
import { execFile } from "node:child_process";

import { API_ORIGIN } from "../support/env";
import { label, logInThroughTheApp, useAddress, useLocale, WEB } from "../support/fe2";
import { streamOf } from "../support/fe3";
import {
  ARRIVAL_BOUND_MS,
  connectedOwner,
  conversationOf,
  freshCustomer,
  inbound,
  text,
} from "../support/fe4";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });

function docker(args: string[]): Promise<void> {
  return new Promise<void>((resolve, reject) =>
    execFile("docker", args, (error) => (error ? reject(new Error(error.message)) : resolve())),
  );
}

test("IJ-08: the API stops with the inbox open, polling while down, recovery with the gap refetch", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const line = await connectedOwner("ij08");
  const sender = freshCustomer();
  await inbound(line, sender, text("before the outage"));
  const conversation = await conversationOf(line.owner, sender);
  const lists: number[] = [];
  context.on("request", (request) => {
    if (request.method() === "GET" && /\/api\/v1\/conversations\?/.test(request.url()))
      lists.push(Date.now());
  });
  await useAddress(context);
  await useLocale(context, "en");
  await logInThroughTheApp(page, "en", line.owner.email);
  await page.waitForURL(`${WEB}/`);
  await page.goto(`${WEB}/inbox/${conversation.id}`);
  await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await streamOf(page))?.state).toBe("open");

  await docker(["stop", "wasla-fe-e2e-api"]);
  const downAt = Date.now();
  try {
    await expect(page.locator('[data-banner="live"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("gate-announcement")).toContainText(
      label("en", "app.live.paused"),
    );
    // Still readable.
    await expect(
      page.locator("[data-message-text]").filter({ hasText: "before the outage" }),
    ).toBeVisible();
    // Polling at the interval (30 s without a stream): at least one list read while down.
    await expect
      .poll(() => lists.filter((at) => at > downAt).length, { timeout: 60_000 })
      .toBeGreaterThanOrEqual(1);
  } finally {
    await docker(["start", "wasla-fe-e2e-api"]);
  }
  // The moment the API answers, the customer writes again.
  await expect
    .poll(async () => (await fetch(`${API_ORIGIN}/health/live`).catch(() => null))?.status ?? 0, {
      timeout: 90_000,
    })
    .toBe(200);
  await inbound(line, sender, text("during the reconnect"));
  await expect.poll(async () => (await streamOf(page))?.state, { timeout: 90_000 }).toBe("open");
  await expect(
    page.locator("[data-message-text]").filter({ hasText: "during the reconnect" }),
  ).toBeVisible({
    timeout: ARRIVAL_BOUND_MS * 2,
  });
  await expect(page.locator('[data-banner="live"]')).toHaveCount(0);
});
