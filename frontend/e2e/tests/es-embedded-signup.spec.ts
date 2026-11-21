/**
 * ADR-204's journeys (EJ-01 .. EJ-14) against FE-0's profile and the Meta Embedded Signup fakes:
 * the fake SDK opens the fake popup, which posts Meta's documented message events and calls the
 * login callback with a code; the API exchanges it with the fakes' Graph. Every wait is on a fact:
 * the fakes' call log, a text on the page, a row read back through the API. Each journey uses its
 * own WhatsApp account id (`-W<n>`), so an account_update for one never touches another.
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import { API_ORIGIN } from "../support/env";
import {
  label,
  logInThroughTheApp,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
  type Account,
} from "../support/fe2";
import { connectNumber, freshNumber, streamOf } from "../support/fe3";
import { conversationOf, freshCustomer, read } from "../support/fe4";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 180_000 });

const locale = "en" as const;
const FAKES = `http://localhost:${process.env["WASLA_E2E_FAKES_PORT"] ?? "8026"}`;
const BOUND_MS = 30_000;

interface MetaCall {
  step: string;
  method: string;
  path: string;
}

async function metaCalls(): Promise<MetaCall[]> {
  const response = await fetch(`${FAKES}/_meta/calls`);
  const body = (await response.json()) as { calls?: MetaCall[] } | MetaCall[];
  return Array.isArray(body) ? body : (body.calls ?? []);
}

/** A journey's own number on its own WhatsApp account (the fake owns every PNID-E2E… id). */
function line(coexistence = false): { number: string; waba: string } {
  const waba = String(700_000_000 + Math.floor(Math.random() * 99_999_999));
  const number = `${freshNumber()}${coexistence ? "-COEX" : ""}-W${waba}`;
  return { number, waba };
}

/** A delivery signed by the fakes as Meta signs it, posted to the API (FK-07). */
async function deliver(
  number: string,
  waba: string,
  field: string,
  value: Record<string, unknown>,
): Promise<void> {
  const payload = {
    object: "whatsapp_business_account",
    entry: [
      {
        id: waba,
        changes: [
          {
            field,
            value: {
              messaging_product: "whatsapp",
              metadata: { display_phone_number: "201000000099", phone_number_id: number },
              ...value,
            },
          },
        ],
      },
    ],
  };
  const response = await fetch(`${FAKES}/_meta/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload }),
  });
  expect(response.status).toBe(200);
}

async function deliverRaw(payload: Record<string, unknown>): Promise<void> {
  const response = await fetch(`${FAKES}/_meta/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload }),
  });
  expect(response.status).toBe(200);
}

const now = () => String(Math.floor(Date.now() / 1000));

async function signIn(page: Page, context: BrowserContext, email: string) {
  await useAddress(context);
  await useLocale(context, locale);
  await logInThroughTheApp(page, locale, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
}

async function axe(page: Page, state: string): Promise<void> {
  await expect(page).toHaveTitle(/\S/);
  await expect
    .poll(async () => (await streamOf(page))?.state ?? "none", { timeout: 20_000 })
    .not.toBe("connecting");
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"])
    .analyze();
  expect(
    result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => `${state}: ${v.id} ${v.help}`),
  ).toEqual([]);
}

type Action = "finish" | "finish-app" | "close" | "cancel" | "error";

/** Opens the adapter, launches the fake popup and presses one of its buttons. */
async function runFlow(
  page: Page,
  options: { number?: string; waba?: string; code?: string; action: Action; keyboard?: boolean },
): Promise<void> {
  if (!page.url().endsWith("/channels/connect/whatsapp-embedded")) {
    await page.goto(`${WEB}/channels/connect/whatsapp-embedded`);
  }
  const launch = page.getByTestId("es-launch");
  await expect(launch).toBeEnabled({ timeout: BOUND_MS });
  const opened = page.waitForEvent("popup");
  if (options.keyboard) {
    await launch.focus();
    await page.keyboard.press("Enter");
  } else {
    await launch.click();
  }
  const popup = await opened;
  await popup.waitForLoadState("domcontentloaded");
  if (options.number) await popup.locator("#number").fill(options.number);
  if (options.code) await popup.locator("#code").fill(options.code);
  if (options.waba) await popup.locator("#waba").fill(options.waba);
  const closed = popup.waitForEvent("close");
  await popup.locator(`button[data-action="${options.action}"]`).click();
  // The fake closes itself; a second window.open with the same name before then would reuse it.
  await closed;
}

async function connected(page: Page): Promise<string> {
  await page.waitForURL(/\/channels\/[0-9a-f-]{36}$/, { timeout: BOUND_MS });
  return page.url().split("/").pop()!;
}

async function accountsOf(owner: Account): Promise<Array<{ id: string; phone_number_id: string }>> {
  const answer = await read<{ accounts?: Array<{ id: string; phone_number_id: string }> }>(
    owner,
    "/api/v1/whatsapp/accounts",
  );
  return answer.accounts ?? [];
}

test.describe("Embedded Signup and Coexistence (ADR-204)", () => {
  test.skip(
    ({ browserName }) => browserName !== "chromium",
    "the stage's light gates run these journeys in Chromium (GitHub runs the rest)",
  );

  test("EJ-01 + EJ-14: a new number through Meta's window, subscribed and registered; nothing kept", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej1");
    const { number, waba } = line();
    const code = `es-ok-${Math.random().toString(36).slice(2, 12)}`;
    const consoleLines: string[] = [];
    const requested: string[] = [];
    page.on("console", (message) => consoleLines.push(message.text()));
    context.on("request", (request) => requested.push(request.url()));
    await signIn(page, context, owner.email);

    await page.goto(`${WEB}/channels/connect/whatsapp-embedded`);
    await expect(page.getByTestId("es-explainer")).toBeVisible();
    await axe(page, "embedded signup explainer");
    await runFlow(page, { number, waba, code, action: "finish" });
    const id = await connected(page);
    await expect(page.getByTestId("onboarding-method").first()).toHaveText(
      label(locale, "app.onboarding.withMeta"),
    );
    await expect(page.getByTestId("onboarding-payment")).toBeVisible();
    await expect(page.getByTestId("onboarding-coexistence")).toHaveCount(0);
    await axe(page, "connection page, new number");

    const mine = (await metaCalls()).filter(
      (call) => call.path.includes(number) || call.path.includes(waba),
    );
    const steps = mine.map((call) => call.step);
    expect(steps).toContain("subscribe");
    expect(steps.indexOf("coexistence")).toBeGreaterThanOrEqual(0);
    expect(steps.indexOf("register")).toBeGreaterThan(steps.indexOf("coexistence"));
    expect(steps).not.toContain("sync:history");
    const onboarding = await read<{ method: string; mode: string; registered_at: string | null }>(
      owner,
      `/api/v1/whatsapp/accounts/${id}/onboarding`,
    );
    expect(onboarding.method).toBe("embedded_signup");
    expect(onboarding.mode).toBe("cloud_api");
    expect(onboarding.registered_at).not.toBeNull();

    // EJ-14: the code is nowhere the browser keeps things, and no token or PIN reached it.
    const kept = await page.evaluate(async () => {
      const databases = (await indexedDB.databases?.()) ?? [];
      return JSON.stringify({
        local: { ...localStorage },
        session: { ...sessionStorage },
        databases: databases.map((database) => database.name),
        url: location.href,
      });
    });
    for (const place of [kept, consoleLines.join("\n"), requested.join("\n")]) {
      expect(place).not.toContain(code);
      expect(place).not.toContain("e2e-es-token");
    }
    expect(await page.locator("body").innerText()).not.toMatch(/e2e-es-token|\bpin\b\s*\d{6}/i);
  });

  test("EJ-02: a WhatsApp Business app number - never registered, synced once each, history complete", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej2");
    const { number, waba } = line(true);
    await signIn(page, context, owner.email);
    await runFlow(page, { number, waba, code: "es-ok-coex", action: "finish-app" });
    await connected(page);
    await expect(page.getByTestId("onboarding-coexistence")).toHaveText(
      label(locale, "app.onboarding.coexistence"),
    );
    await expect(page.getByTestId("onboarding-reminder")).toBeVisible();
    await expect
      .poll(
        async () =>
          (await metaCalls())
            .filter((call) => call.path.includes(number))
            .map((call) => call.step)
            .filter((step) => step.startsWith("sync:"))
            .sort(),
        { timeout: 60_000 },
      )
      .toEqual(["sync:history", "sync:smb_app_state_sync"]);
    expect(
      (await metaCalls()).filter((call) => call.path.includes(number) && call.step === "register"),
    ).toEqual([]);

    await deliver(number, waba, "history", {
      history: [
        {
          metadata: { phase: 0, chunk_order: 1, progress: 100 },
          threads: [
            {
              id: freshCustomer(),
              messages: [
                {
                  from: "201000000099",
                  to: "201055500001",
                  id: `wamid.HIS${Date.now()}`,
                  timestamp: String(Math.floor(Date.now() / 1000) - 86_400),
                  type: "text",
                  text: { body: "Imported line" },
                  history_context: { status: "read" },
                },
              ],
            },
          ],
        },
      ],
    });
    await expect(page.getByTestId("onboarding")).toContainText(
      label(locale, "app.onboarding.historyComplete"),
      { timeout: 60_000 },
    );
    await axe(page, "connection page, coexistence complete");
  });

  test("EJ-03: a declined history share reads as the owner's choice", async ({ page, context }) => {
    const owner = await verifiedOwner("ej3");
    const { number, waba } = line(true);
    await signIn(page, context, owner.email);
    await runFlow(page, { number, waba, code: "es-ok-declined", action: "finish-app" });
    await connected(page);
    await deliver(number, waba, "history", {
      history: [
        {
          errors: [
            {
              code: 2593109,
              title: "History sync is turned off by the business from the WhatsApp Business App",
            },
          ],
        },
      ],
    });
    await expect(page.getByTestId("onboarding")).toContainText(
      label(locale, "app.onboarding.historyDeclined"),
      { timeout: 60_000 },
    );
  });

  test("EJ-04: closing Meta's window at a step says which, and the button opens it again (keyboard)", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej4");
    await signIn(page, context, owner.email);
    await runFlow(page, { action: "cancel", keyboard: true });
    await expect(page.getByTestId("es-cancelled")).toHaveText(
      label(locale, "app.embedded.cancelled").replace(
        "{step}",
        label(locale, "app.embedded.steps.verifyNumber"),
      ),
    );
    await expect(page.locator('[data-page="connect-embedded"]')).toBeVisible();
    expect(page.url()).toMatch(/\/channels\/connect\/whatsapp-embedded$/);
    const again = page.waitForEvent("popup");
    await page.getByTestId("es-launch").focus();
    await page.keyboard.press("Enter");
    await (await again).close();
    expect(await accountsOf(owner)).toEqual([]);
  });

  test("EJ-05: Meta's region error gets its sentence and a session id to copy", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej5");
    await signIn(page, context, owner.email);
    await runFlow(page, { action: "error" });
    const failed = page.getByTestId("es-failed");
    await expect(failed).toContainText(label(locale, "app.embedded.errors.region"));
    await expect(failed).toContainText("e2e-session-3441042");
    await expect(
      failed.getByRole("button", { name: label(locale, "app.embedded.copySession") }),
    ).toBeVisible();
    await axe(page, "embedded signup error");
  });

  test("EJ-06 + EJ-07: an expired code and a number the token cannot prove create nothing", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej6");
    await signIn(page, context, owner.email);
    const expired = line();
    await runFlow(page, { ...expired, code: "es-expired", action: "finish" });
    await expect(page.locator('[data-error-code="embedded_signup_code_expired"]')).toBeVisible({
      timeout: BOUND_MS,
    });
    expect(await accountsOf(owner)).toEqual([]);

    await runFlow(page, {
      number: `PNID-NOT-OURS-${Date.now()}`,
      waba: expired.waba,
      code: "es-ok-unproved",
      action: "finish",
    });
    await expect(page.locator('[data-error-code="whatsapp_ownership_unverified"]')).toContainText(
      label(locale, "errors.whatsapp_ownership_unverified"),
      { timeout: BOUND_MS },
    );
    expect(await accountsOf(owner)).toEqual([]);
  });

  test("EJ-08: at capacity the button is not offered and Meta is never opened", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej8");
    const held = freshNumber();
    expect((await connectNumber(owner, held)).status).toBe(201);
    await signIn(page, context, owner.email);
    let opened = false;
    page.on("popup", () => {
      opened = true;
    });
    await page.goto(`${WEB}/channels/connect/whatsapp-embedded`);
    await expect(page.getByTestId("connect-reason")).toHaveText(
      label(locale, "app.channels.noSlots"),
    );
    await expect(page.getByTestId("es-launch")).toHaveCount(0);
    expect(opened).toBe(false);
  });

  test("EJ-09 + EJ-10: an app reply is shown as such and stops the AI; an edit and a revoke", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej9");
    const { number, waba } = line(true);
    const customer = freshCustomer();
    await signIn(page, context, owner.email);
    await runFlow(page, { number, waba, code: "es-ok-echo", action: "finish-app" });
    await connected(page);

    const original = `wamid.ORIG${Date.now()}`;
    await deliver(number, waba, "messages", {
      contacts: [{ profile: { name: "Mona" }, wa_id: customer }],
      messages: [
        { from: customer, id: original, timestamp: now(), type: "text", text: { body: "2 rooms" } },
      ],
    });
    const conversation = await conversationOf(owner, customer);
    await deliver(number, waba, "smb_message_echoes", {
      message_echoes: [
        {
          from: "201000000099",
          to: customer,
          id: `wamid.ECHO${Date.now()}`,
          timestamp: now(),
          type: "text",
          text: { body: "Replied from the app" },
        },
      ],
    });
    await page.goto(`${WEB}/inbox/${conversation.id}`);
    await expect(page.locator('[data-mark="business-app"]').first()).toBeVisible({
      timeout: BOUND_MS,
    });
    await expect
      .poll(async () => (await conversationOf(owner, customer)).mode, { timeout: BOUND_MS })
      .toBe("human");

    await deliver(number, waba, "messages", {
      messages: [
        {
          from: customer,
          id: `wamid.EDIT${Date.now()}`,
          timestamp: now(),
          type: "edit",
          edit: {
            original_message_id: original,
            message: { type: "text", text: { body: "3 rooms" } },
          },
        },
      ],
    });
    await expect(page.locator('[data-mark="edited"]').first()).toBeVisible({ timeout: BOUND_MS });
    await deliver(number, waba, "messages", {
      messages: [
        {
          from: customer,
          id: `wamid.REVOKE${Date.now()}`,
          timestamp: now(),
          type: "revoke",
          revoke: { original_message_id: original },
        },
      ],
    });
    await expect(page.locator('[data-mark="revoked"]').first()).toHaveText(
      label(locale, "app.inbox.coexistence.revoked"),
      { timeout: BOUND_MS },
    );
    await axe(page, "transcript with coexistence marks");
  });

  test("EJ-11: Meta removes the partner after ~14 days unopened - the page, home and Connect again", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej11");
    const { number, waba } = line(true);
    await signIn(page, context, owner.email);
    await runFlow(page, { number, waba, code: "es-ok-removed", action: "finish-app" });
    const id = await connected(page);
    await deliverRaw({
      object: "whatsapp_business_account",
      entry: [
        {
          id: "2949482758682047",
          time: Math.floor(Date.now() / 1000),
          changes: [
            {
              field: "account_update",
              value: {
                event: "PARTNER_REMOVED",
                waba_info: { waba_id: waba, owner_business_id: "2329417887457253" },
                disconnection_info: { reason: "PRIMARY_INACTIVITY", initiated_by: "SYSTEM" },
              },
            },
          ],
        },
      ],
    });
    await expect(page.getByTestId("onboarding-disconnect-reason")).toHaveText(
      label(locale, "app.onboarding.reasons.PRIMARY_INACTIVITY"),
      { timeout: BOUND_MS },
    );
    await expect(page.getByTestId("onboarding-reconnect")).toBeVisible();
    await axe(page, "connection page, provider disconnected");
    await page.goto(`${WEB}/`);
    await expect(page.locator('[data-banner="number-disconnected"]')).toBeVisible({
      timeout: BOUND_MS,
    });
    expect(id).toMatch(/[0-9a-f-]{36}/);
  });

  test("EJ-12: release unsubscribes and never deregisters; the same flow links it again", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("ej12");
    const { number, waba } = line();
    await signIn(page, context, owner.email);
    await runFlow(page, { number, waba, code: "es-ok-release", action: "finish" });
    await connected(page);

    await page.getByRole("button", { name: label(locale, "app.detail.release") }).click();
    const dialog = page.getByRole("dialog");
    const digits = (await page.locator('[data-key="number"] bdi').innerText())
      .replace(/\D/g, "")
      .slice(-4);
    await dialog.getByRole("textbox").fill(digits);
    await dialog.getByRole("button", { name: label(locale, "app.detail.release") }).click();
    await page.waitForURL(/\/channels\?notice=released$/, { timeout: BOUND_MS });
    await expect
      .poll(async () =>
        (await metaCalls()).filter((call) => call.path.includes(waba)).map((call) => call.step),
      )
      .toContain("unsubscribe");
    expect(
      (await metaCalls()).filter(
        (call) => call.path.includes(number) && /deregister/.test(call.path),
      ),
    ).toEqual([]);

    await runFlow(page, { number, waba, code: "es-ok-relink", action: "finish" });
    const again = await connected(page);
    await expect(page.getByTestId("onboarding-method").first()).toHaveText(
      label(locale, "app.onboarding.withMeta"),
    );
    expect(again).toMatch(/[0-9a-f-]{36}/);
  });

  test("EJ-13: the access-token form stays one click away", async ({ page, context }) => {
    const owner = await verifiedOwner("ej13");
    await signIn(page, context, owner.email);
    await page.goto(`${WEB}/channels/connect/whatsapp-embedded`);
    await page.getByTestId("es-assisted").click();
    await page.waitForURL(`${WEB}/channels/connect/whatsapp`);
    await expect(page.getByTestId("connect-guide")).toBeAttached();
    expect(API_ORIGIN).toMatch(/^http:\/\/localhost:/);
  });
});
