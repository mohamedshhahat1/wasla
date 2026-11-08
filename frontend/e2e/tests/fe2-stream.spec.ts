/**
 * FX-07 against FE-0's real API (PV-03's scenario, automated): the stream helper opened from
 * apps/web's origin, then `docker restart wasla-fe-e2e-api`; the helper takes a NEW ticket and
 * the next inbound message.created arrives. Runs alone, after every other project (it restarts
 * the API), in Chromium only - the rule under test is the helper's, not the engine's.
 */
import { execFileSync } from "node:child_process";
import { createHmac, randomUUID } from "node:crypto";

import type { Page } from "@playwright/test";

import { API_ORIGIN } from "../support/env";
import { field, label, useAddress, WEB } from "../support/fe2";
import { expect, test } from "../support/page-guard";

const DEMO_EMAIL = "demo.owner.e2e@example.com";
const DEMO_PASSWORD = "e2e-demo-password-NOT-A-SECRET";
/** The e2e stack's PUBLIC Meta app secret (docker-compose.e2e.yml), never a deployment's. */
const META_APP_SECRET = "e2e-meta-app-secret-NOT-A-SECRET";

type Entry = { kind?: string; type?: string; ticketNumber?: number };

const log = (page: Page) =>
  page.evaluate(() => (window as unknown as { __waslaStreamLog: Entry[] }).__waslaStreamLog);

async function inbound(text: string): Promise<number> {
  const payload = {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "555000111",
        changes: [
          {
            field: "messages",
            value: {
              messaging_product: "whatsapp",
              metadata: { display_phone_number: "201000000000", phone_number_id: "PNID-E2E-DEMO" },
              contacts: [{ profile: { name: "Stream" }, wa_id: "201234567890" }],
              messages: [
                {
                  from: "201234567890",
                  id: `wamid.FX07${randomUUID().slice(0, 8)}`,
                  timestamp: String(Math.floor(Date.now() / 1000)),
                  type: "text",
                  text: { body: text },
                },
              ],
            },
          },
        ],
      },
    ],
  };
  const raw = JSON.stringify(payload);
  const signature = `sha256=${createHmac("sha256", META_APP_SECRET).update(raw).digest("hex")}`;
  const response = await fetch(`${API_ORIGIN}/api/v1/webhooks/whatsapp`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Hub-Signature-256": signature },
    body: raw,
  });
  return response.status;
}

async function healthy(): Promise<void> {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      if ((await fetch(`${API_ORIGIN}/health/ready`)).ok) return;
    } catch {
      // restarting
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("the e2e API did not come back");
}

test("after an API restart the helper takes a new ticket and the next frame arrives", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  execFileSync(
    "docker",
    ["exec", "wasla-fe-e2e-api", "python", "-m", "tests.e2e_stack.seed", "--demo"],
    {
      stdio: "ignore",
    },
  );
  await useAddress(context);
  const tickets: number[] = [];
  page.on("request", (request) => {
    if (request.url() === `${API_ORIGIN}/api/v1/events/ticket`) tickets.push(Date.now());
  });

  await page.goto(`${WEB}/login`);
  await field(page, "en", "auth.email").fill(DEMO_EMAIL);
  await field(page, "en", "auth.password").fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: label("en", "auth.login.submit") }).click();
  await page.waitForURL(`${WEB}/`);
  await page.goto(`${WEB}/test-only/session`);
  await expect(page.getByTestId("session-hooks")).toHaveAttribute("data-status", "signed-in");
  await page.evaluate(() => (window as unknown as { __waslaStream(): void }).__waslaStream());
  await expect.poll(async () => (await log(page)).some((e) => e.kind === "open")).toBe(true);

  expect(await inbound("before the restart")).toBe(200);
  await expect
    .poll(async () => (await log(page)).filter((e) => e.type === "message.created").length, {
      timeout: 30_000,
    })
    .toBe(1);

  const ticketsBefore = tickets.length;
  const opensBefore = (await log(page)).filter((e) => e.kind === "open").length;
  execFileSync("docker", ["restart", "wasla-fe-e2e-api"], { stdio: "ignore" });
  await healthy();

  await expect
    .poll(async () => (await log(page)).filter((e) => e.kind === "open").length, {
      timeout: 90_000,
    })
    .toBeGreaterThan(opensBefore);
  expect(tickets.length, "a new ticket after the restart").toBeGreaterThan(ticketsBefore);
  const reopened = (await log(page)).filter((e) => e.kind === "open").at(-1)!;
  expect(reopened.ticketNumber).toBeGreaterThan(1);

  expect(await inbound("after the restart")).toBe(200);
  await expect
    .poll(async () => (await log(page)).filter((e) => e.type === "message.created").length, {
      timeout: 30_000,
    })
    .toBe(2);
  const timeline = (await log(page)).map((e) => e.kind ?? e.type).join(" → ");
  test.info().annotations.push({ type: "fx07-timeline", description: timeline });
});
