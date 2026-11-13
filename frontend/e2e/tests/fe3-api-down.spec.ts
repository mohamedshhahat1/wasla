/**
 * FE-3's JR-11: the API stops - the shell shows "paused - retrying" once; it starts - live again
 * with a new ticket. Stopping the API would fail every journey running beside it, so this file has
 * a project of its own ("api-down", playwright.config.ts) that runs alone, after FX-07's restart.
 */
import { execFile } from "node:child_process";

import {
  label,
  logInThroughTheApp,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
} from "../support/fe2";
import { streamOf } from "../support/fe3";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });

function docker(args: string[]): Promise<void> {
  return new Promise<void>((resolve, reject) =>
    execFile("docker", args, (error) => (error ? reject(new Error(error.message)) : resolve())),
  );
}

test("live updates down and back (JR-11): the API stops, paused - retrying; it starts, live again with a new ticket", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const owner = await verifiedOwner("jr11");
  const tickets: string[] = [];
  context.on("request", (request) => {
    if (request.url().includes("/events/ticket")) tickets.push(request.url());
  });
  await useAddress(context);
  await useLocale(context, "en");
  await logInThroughTheApp(page, "en", owner.email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await streamOf(page))?.state).toBe("open");

  await docker(["stop", "wasla-fe-e2e-api"]);
  try {
    await expect(page.locator('[data-banner="live"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("gate-announcement")).toContainText(
      label("en", "app.live.paused"),
    );
  } finally {
    await docker(["start", "wasla-fe-e2e-api"]);
  }
  await expect.poll(async () => (await streamOf(page))?.state, { timeout: 90_000 }).toBe("open");
  await expect(page.locator('[data-banner="live"]')).toHaveCount(0);
  expect(tickets.length).toBeGreaterThanOrEqual(2);
});
