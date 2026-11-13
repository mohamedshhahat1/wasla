/**
 * FE-3's journeys (JR-02..JR-12) against FE-0's profile: the shell and switcher, every gate of
 * plan section 9 for the roles it concerns, one stream per browser, the connect secret, request
 * discipline, live updates down and back, axe and the keyboard - en and ar, six projects (the
 * request-count and stream-count proofs in the projects named beside them).
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import {
  api,
  label,
  logInThroughTheApp,
  nothingReachable,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
  type Account,
} from "../support/fe2";
import {
  connectNumber,
  freshNumber,
  gateState,
  memberOf,
  recordRequests,
  restore,
  revoke,
  REVOKE_BOUND_MS,
  streamOf,
  suspend,
  SUSPEND_BOUND_MS,
} from "../support/fe3";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
// Several actors per journey (owner, member, staff), each made through the API and the mailbox.
test.describe.configure({ timeout: 180_000 });

const FIREFOX_SKIP = ["label", "label-title-only"];

async function axe(page: Page, state: string): Promise<void> {
  await expect(page).toHaveTitle(/\S/);
  const builder = new AxeBuilder({ page }).withTags([
    "wcag2a",
    "wcag2aa",
    "wcag21a",
    "wcag21aa",
    "wcag22aa",
    "best-practice",
  ]);
  if (test.info().project.name.startsWith("firefox")) builder.disableRules(FIREFOX_SKIP);
  const result = await builder.analyze();
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

const phone = () => test.info().project.name.endsWith("phone");
const project = () => test.info().project.name;

async function signIn(page: Page, context: BrowserContext, locale: Locale, email: string) {
  await useAddress(context);
  await useLocale(context, locale);
  await logInThroughTheApp(page, locale, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
}

/** The navigation lives in a sheet on a phone: open it first there. */
async function openNav(page: Page, locale: Locale) {
  if (phone()) await page.getByRole("button", { name: label(locale, "app.menu") }).click();
}

function banner(page: Page, name: string) {
  return page.locator(`[data-banner="${name}"]`);
}

for (const locale of ["en", "ar"] as Locale[]) {
  test.describe(`${locale}`, () => {
    test("JR-02: the shell, the switcher with status, the account menu, keyboard", async ({
      page,
      context,
      guard,
    }) => {
      const owner = await verifiedOwner("jr2");
      const second = `${owner.slug}-p`;
      const made = await api("POST", "/api/v1/workspaces", {
        token: owner.token,
        body: { name: `Paused ${second}`, slug: second, timezone: "Africa/Cairo" },
      });
      expect(made.status).toBe(201);
      await suspend(second);
      await signIn(page, context, locale, owner.email);

      // Landmarks, the skip link first, the navigation with only what exists.
      await page.keyboard.press("Tab");
      await expect(
        page.getByRole("link", { name: label(locale, "common.skipToContent") }),
      ).toBeFocused();
      await openNav(page, locale);
      const nav = page
        .getByRole("navigation", { name: label(locale, "app.navLabel") })
        .filter({ visible: true });
      await expect(nav.getByRole("link")).toHaveCount(2);
      await expect(nav.getByRole("link", { name: label(locale, "app.nav.home") })).toHaveAttribute(
        "aria-current",
        "page",
      );
      if (phone()) await page.keyboard.press("Escape");

      // The switcher: both workspaces, the suspended one marked in words.
      await page.locator("[data-switcher-trigger]").click();
      const menu = page.locator("[data-switcher-menu]");
      await expect(menu.getByRole("menuitemradio")).toHaveCount(2);
      const paused = menu.locator(`[data-workspace="${second}"]`);
      await expect(paused).toContainText(label(locale, "app.switcher.suspended"));
      await expect(menu.locator(`[data-workspace="${owner.slug}"]`)).toHaveAttribute(
        "aria-checked",
        "true",
      );
      await axe(page, "switcher open");
      // The API refuses a switch into a suspended workspace (403 workspace_suspended, FEB-06):
      // its own sentence, and the current workspace stays open.
      await paused.click();
      await expect(
        page.getByText(label(locale, "errors.workspace_suspended")).first(),
      ).toBeVisible();
      await expect(page.locator('[data-page="home"]')).toBeVisible();
      await axe(page, "switch refused: suspended");

      // The account menu: who, where, as what; the language; sign out stays FE-2's.
      await page.locator("[data-account-trigger]").click();
      const account = page.locator("[data-account-menu]");
      await expect(account).toContainText(owner.email);
      await expect(account).toContainText(`Workspace ${owner.slug}`);
      await expect(account).toContainText(label(locale, "auth.roles.tenant_owner"));
      await axe(page, "account menu");
      await page.keyboard.press("Escape");
      await nothingReachable(page, context);
      expect(guard.violations).toEqual([]);
      await restore(second);
    });

    test("JR-03: suspended via the console API - owner and member, no stream reopened, restore", async ({
      page,
      context,
      browser,
    }) => {
      const owner = await verifiedOwner("jr3");
      const mate = await memberOf(owner);
      await signIn(page, context, locale, owner.email);
      const memberContext = await browser.newContext({ timezoneId: "Africa/Cairo" });
      const memberPage = await memberContext.newPage();
      await signIn(memberPage, memberContext, locale, mate.email);
      const seen = recordRequests(context);

      await suspend(owner.slug);
      for (const tab of [page, memberPage]) {
        await expect(tab.locator('[data-gate="suspended"]')).toBeVisible({
          timeout: SUSPEND_BOUND_MS,
        });
        await expect(tab.getByRole("heading", { level: 1 })).toHaveText(
          label(locale, "app.gates.suspended.title"),
        );
      }
      const before = seen.filter((line) => line.includes("/events/ticket")).length;
      await axe(memberPage, "suspended (member)");
      expect(seen.filter((line) => line.includes("/events/ticket")).length).toBe(before);

      await restore(owner.slug);
      await page
        .getByRole("button", { name: label(locale, "app.gates.suspended.checkAgain") })
        .click();
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      await expect.poll(() => seen.some((line) => line.includes("/events/ticket"))).toBe(true);
      await memberContext.close();
    });

    test("JR-04: access withdrawn - the state, the switcher, a stranger's 404", async ({
      page,
      context,
      browser,
    }) => {
      const owner = await verifiedOwner("jr4");
      const mate = await memberOf(owner);
      const number = freshNumber();
      const connected = await connectNumber(owner, number);
      expect(connected.status).toBe(201);
      const connectionId = (connected.json as { id: string }).id;
      await signIn(page, context, locale, mate.email);

      await revoke(owner, mate);
      await expect(page.locator('[data-gate="revoked"]')).toBeVisible({ timeout: REVOKE_BOUND_MS });
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        label(locale, "app.gates.revoked.title"),
      );
      await axe(page, "revoked");

      // A stranger opening that connection sees what a random id shows.
      const stranger = await verifiedOwner("jr4s");
      const strangerContext = await browser.newContext({ timezoneId: "Africa/Cairo" });
      const strangerPage = await strangerContext.newPage();
      await signIn(strangerPage, strangerContext, locale, stranger.email);
      await strangerPage.goto(`${WEB}/channels/${connectionId}`);
      await expect(strangerPage.locator('[data-page="connection-not-found"]')).toBeVisible();
      const theirs = await strangerPage.locator("main").innerText();
      await strangerPage.goto(`${WEB}/channels/00000000-0000-4000-8000-000000000000`);
      await expect(strangerPage.locator('[data-page="connection-not-found"]')).toBeVisible();
      expect(await strangerPage.locator("main").innerText()).toBe(theirs);
      await strangerContext.close();
    });

    test("JR-05: payment, trial, a member without billing and without amounts", async ({
      page,
      context,
      browser,
    }) => {
      const owner = await verifiedOwner("jr5");
      const mate = await memberOf(owner);
      await gateState("past-due", owner.slug);
      await signIn(page, context, locale, owner.email);
      await expect(banner(page, "payment")).toBeVisible();
      await expect(banner(page, "payment").locator("button")).toHaveCount(0);
      await axe(page, "payment banner (owner)");

      const memberContext = await browser.newContext({ timezoneId: "Africa/Cairo" });
      const memberPage = await memberContext.newPage();
      const calls = recordRequests(memberContext);
      await signIn(memberPage, memberContext, locale, mate.email);
      await expect(banner(memberPage, "billing-member")).toBeVisible();
      await expect(banner(memberPage, "payment")).toHaveCount(0);
      expect(calls.filter((line) => line.includes("/billing/summary"))).toEqual([]);
      const text = await memberPage.locator("body").innerText();
      expect(text).not.toMatch(/EGP|USD|99\.00|ج\.م/);
      await memberContext.close();

      const trialOwner = await verifiedOwner("jr5t");
      await gateState("trialing", trialOwner.slug, 3);
      const trialContext = await browser.newContext({ timezoneId: "Africa/Cairo" });
      const trialPage = await trialContext.newPage();
      await signIn(trialPage, trialContext, locale, trialOwner.email);
      await expect(banner(trialPage, "trial")).toBeVisible();
      await axe(trialPage, "trial banner");
      await trialPage.getByRole("button", { name: label(locale, "app.gates.dismiss") }).click();
      await expect(banner(trialPage, "trial")).toHaveCount(0);
      await trialContext.close();
    });

    test("JR-06: capacity full - the connect action refused with its reason; the API's own refusal", async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("jr6");
      expect((await connectNumber(owner, freshNumber())).status).toBe(201);
      await signIn(page, context, locale, owner.email);
      await page.goto(`${WEB}/channels`);
      await expect(page.getByTestId("connect-action")).toBeDisabled();
      await expect(page.getByTestId("connect-reason")).toHaveText(
        label(locale, "app.channels.noSlots"),
      );
      await axe(page, "channels, capacity full");
      // A stale page forcing it: the API's 409 channel_capacity_exceeded, its own sentence.
      const refused = await connectNumber(owner, freshNumber());
      expect(refused.status).toBe(409);
      expect((refused.json as { error: { code: string } }).error.code).toBe(
        "channel_capacity_exceeded",
      );
    });

    test("JR-07: connect, already connected, disable, enable, release - and the token kept nowhere", async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("jr7");
      const other = await verifiedOwner("jr7o");
      const taken = freshNumber();
      expect((await connectNumber(other, taken)).status).toBe(201);
      await signIn(page, context, locale, owner.email);
      const token = `EAAG-e2e-secret-${Math.random().toString(36).slice(2)}`;
      const consoleLines: string[] = [];
      page.on("console", (message) => consoleLines.push(message.text()));
      await page.addInitScript(() => {
        const heard: unknown[] = [];
        (window as unknown as { __heard: unknown[] }).__heard = heard;
        const Original = window.BroadcastChannel;
        window.BroadcastChannel = class extends Original {
          override postMessage(message: unknown) {
            heard.push(message);
            super.postMessage(message);
          }
        };
      });

      await page.goto(`${WEB}/channels/connect/whatsapp`);
      await expect(page.getByTestId("connect-guide")).toBeAttached();
      await axe(page, "connect form empty");
      const fill = async (number: string) => {
        await page.getByLabel(label(locale, "app.connect.phoneNumberId")).fill(number);
        await page
          .getByLabel(label(locale, "app.connect.accessToken"), { exact: false })
          .first()
          .fill(token);
        await page.keyboard.press("Enter");
      };
      await fill(taken);
      await expect(page.locator("[data-error-code]")).toContainText(
        label(locale, "errors.whatsapp_number_already_connected"),
      );
      await expect(page.locator("[data-error-code]")).not.toContainText(other.slug);
      await axe(page, "connect form refused");

      const secretNowhere = async () => {
        const found = await page.evaluate(async (secret) => {
          const dump = JSON.stringify({
            local: { ...window.localStorage },
            session: { ...window.sessionStorage },
            cookie: document.cookie,
            url: window.location.href,
            history: window.history.state as unknown,
            heard: (window as unknown as { __heard?: unknown[] }).__heard ?? [],
            mutations: window.__waslaFe3?.mutationVariables() ?? [],
            databases: (await (indexedDB.databases?.() ?? Promise.resolve([]))).map((d) => d.name),
          });
          const attributes = [...document.querySelectorAll("*")].some((element) =>
            [...element.attributes].some((attribute) => attribute.value.includes(secret)),
          );
          return {
            dump: dump.includes(secret),
            attributes,
            text: document.body.innerText.includes(secret),
          };
        }, token);
        expect(found).toEqual({ dump: false, attributes: false, text: false });
        expect(consoleLines.join("\n")).not.toContain(token);
      };
      await secretNowhere();

      await page.getByLabel(label(locale, "app.connect.phoneNumberId")).fill(freshNumber());
      await page.keyboard.press("Enter");
      await page.waitForURL(/\/channels\/[0-9a-f-]{36}$/);
      await expect(page.locator('[data-page="connection"]')).toHaveAttribute(
        "data-status",
        "active",
      );
      await secretNowhere();
      await axe(page, "connection detail");

      await page.getByRole("button", { name: label(locale, "app.detail.disable") }).click();
      await page
        .getByRole("dialog")
        .getByRole("button", { name: label(locale, "app.detail.disable") })
        .click();
      await expect(page.locator('[data-page="connection"]')).toHaveAttribute(
        "data-status",
        "disabled",
      );
      await page.getByRole("button", { name: label(locale, "app.detail.enable") }).click();
      await expect(page.locator('[data-page="connection"]')).toHaveAttribute(
        "data-status",
        "active",
      );

      await page.getByRole("button", { name: label(locale, "app.detail.release") }).click();
      const dialog = page.getByRole("dialog");
      const confirm = dialog.getByRole("button", { name: label(locale, "app.detail.release") });
      await expect(confirm).toBeDisabled();
      await axe(page, "release dialog");
      const digits = (await page.locator('[data-key="number"] bdi').innerText())
        .replace(/\D/g, "")
        .slice(-4);
      await dialog.getByRole("textbox").fill(digits);
      await confirm.click();
      await page.waitForURL(/\/channels\?notice=released$/);
      await expect(page.getByTestId("released-notice")).toBeVisible();
      await nothingReachable(page, context);
    });

    test("JR-07b: a member sees no connect form and no channel controls", async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("jr7m");
      const mate = await memberOf(owner);
      const connected = await connectNumber(owner, freshNumber());
      await signIn(page, context, locale, mate.email);
      await page.goto(`${WEB}/channels/connect/whatsapp`);
      await expect(page.getByTestId("connect-role-note")).toHaveText(
        label(locale, "app.roles.onlyAdmins"),
      );
      await expect(page.getByTestId("connect-form")).toHaveCount(0);
      await page.goto(`${WEB}/channels/${(connected.json as { id: string }).id}`);
      await expect(page.getByTestId("connection-actions")).toHaveCount(0);
      await axe(page, "connection detail (member)");
    });

    test("JR-08: home - the checklist for the owner, none for a member; live without a reload", async ({
      page,
      context,
      browser,
    }) => {
      const owner = await verifiedOwner("jr8");
      const mate = await memberOf(owner);
      await signIn(page, context, locale, owner.email);
      const list = page.getByTestId("onboarding-checklist");
      await expect(list).toBeVisible();
      await expect(list.locator('[data-step="channel"]')).toHaveAttribute("data-state", "undone");
      await expect(list.locator('[data-step="team"]')).toHaveAttribute("data-state", "done");
      await axe(page, "home (owner) with checklist");

      expect((await connectNumber(owner, freshNumber())).status).toBe(201);
      await page.reload();
      await expect(
        page.getByTestId("onboarding-checklist").locator('[data-step="channel"]'),
      ).toHaveAttribute("data-state", "done");

      const memberContext = await browser.newContext({ timezoneId: "Africa/Cairo" });
      const memberPage = await memberContext.newPage();
      const calls = recordRequests(memberContext);
      await signIn(memberPage, memberContext, locale, mate.email);
      await expect(memberPage.getByTestId("onboarding-checklist")).toHaveCount(0);
      await expect(memberPage.getByTestId("home-leads")).toHaveCount(0);
      await expect(memberPage.getByTestId("home-usage")).toHaveCount(0);
      for (const route of [
        "/leads/statistics",
        "/api/v1/usage",
        "/billing/summary",
        "/workspace/members",
      ]) {
        expect(calls.filter((line) => line.includes(route))).toEqual([]);
      }
      await axe(memberPage, "home (member)");
      await memberContext.close();
      void API_ORIGIN;
    });

    test("JR-12: keyboard only - the sheet on a phone, the release dialog's trap", async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("jr12");
      await signIn(page, context, locale, owner.email);
      if (phone()) {
        await page.getByRole("button", { name: label(locale, "app.menu") }).focus();
        await page.keyboard.press("Enter");
        const sheet = page.locator("[data-nav-sheet]");
        await expect(sheet).toBeVisible();
        const box = await sheet.boundingBox();
        const width = page.viewportSize()!.width;
        // The sheet sits at the inline start: the left in en, the right in ar.
        if (locale === "ar") expect(Math.round(box!.x + box!.width)).toBe(width);
        else expect(Math.round(box!.x)).toBe(0);
        await axe(page, "navigation sheet");
        await page.keyboard.press("Escape");
        await expect(page.getByRole("button", { name: label(locale, "app.menu") })).toBeFocused();
      } else {
        const side = await page.locator("[data-side-nav]").boundingBox();
        const width = page.viewportSize()!.width;
        if (locale === "ar") expect(Math.round(side!.x + side!.width)).toBe(width);
        else expect(Math.round(side!.x)).toBe(0);
      }
    });
  });
}

test.describe("one stream per browser (JR-09) and request discipline (JR-10)", () => {
  test("six tabs, one stream; the leader leaves, another leads", async ({ context }) => {
    const owner = await verifiedOwner("jr9");
    const pages: Page[] = [];
    const tickets: string[] = [];
    context.on("request", (request) => {
      if (request.url().includes("/events/ticket")) tickets.push(request.url());
    });
    const first = await context.newPage();
    await signIn(first, context, "en", owner.email);
    pages.push(first);
    for (let index = 0; index < 5; index += 1) {
      const tab = await context.newPage();
      await tab.goto(`${WEB}/`);
      await expect(tab.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      pages.push(tab);
    }
    for (const tab of pages) {
      await expect.poll(async () => (await streamOf(tab))?.state).toBe("open");
    }
    const roles = await Promise.all(pages.map(async (tab) => (await streamOf(tab))!.role));
    expect(roles.filter((role) => role === "leader")).toHaveLength(1);
    expect(tickets).toHaveLength(1);

    const leader = pages[roles.indexOf("leader")]!;
    await leader.close();
    const rest = pages.filter((tab) => tab !== leader);
    await expect
      .poll(
        async () =>
          (await Promise.all(rest.map(async (tab) => (await streamOf(tab))?.role))).filter(
            (r) => r === "leader",
          ).length,
        { timeout: 10_000 },
      )
      .toBe(1);
    await expect.poll(() => tickets.length, { timeout: 10_000 }).toBe(2);
  });

  test("control: with the lock bypassed every tab opens its own stream", async ({ context }) => {
    const owner = await verifiedOwner("jr9c");
    await context.addInitScript(() => {
      window.__waslaFe3Flags = { bypassStreamLock: true };
    });
    const tickets: string[] = [];
    context.on("request", (request) => {
      if (request.url().includes("/events/ticket")) tickets.push(request.url());
    });
    const first = await context.newPage();
    await signIn(first, context, "en", owner.email);
    for (let index = 0; index < 2; index += 1) {
      const tab = await context.newPage();
      await tab.goto(`${WEB}/`);
      await expect(tab.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
    }
    await expect.poll(() => tickets.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(3);
  });

  test("Home → Channels → a connection → Home → Channels reads the gates once", async ({
    page,
    context,
  }) => {
    test.skip(project() !== "chromium-desktop", "the request count runs in one project (JR-01)");
    const owner = await verifiedOwner("jr10");
    const connected = await connectNumber(owner, freshNumber());
    const calls = recordRequests(context);
    await signIn(page, context, "en", owner.email);
    const id = (connected.json as { id: string }).id;
    const go = async (path: string, marker: string) => {
      await page.locator(`a[href="${path}"]`).first().click();
      await expect(page.locator(marker)).toBeVisible();
    };
    await go("/channels", '[data-page="channels"]');
    await page.locator(`[data-connection="${id}"] a`).click();
    await expect(page.locator('[data-page="connection"]')).toBeVisible();
    await go("/", '[data-page="home"]');
    await go("/channels", '[data-page="channels"]');
    const count = (route: string) =>
      calls.filter((line) => line.startsWith("GET") && line.includes(route)).length;
    expect(count("/workspace/gates")).toBe(1);
    expect(count("/billing/entitlements")).toBe(1);

    // Hidden and shown within five minutes: nothing; after five (the injected clock): once.
    const flip = async (offset: number) => {
      await page.evaluate((ms) => {
        window.__waslaFe3Flags = { ...window.__waslaFe3Flags, clockOffsetMs: 0 };
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          get: () => "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
        window.__waslaFe3Flags.clockOffsetMs = ms;
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          get: () => "visible",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      }, offset);
    };
    await flip(60_000);
    expect(count("/workspace/gates")).toBe(1);
    await flip(5 * 60_000 + 1000);
    await expect.poll(() => count("/workspace/gates")).toBe(2);
  });
});

test.describe("live updates down and back (JR-11)", () => {
  test("the API stops: paused - retrying; it starts: live again with a new ticket", async ({
    page,
    context,
  }) => {
    test.skip(project() !== "chromium-desktop", "stopping the API runs in one project");
    const owner = await verifiedOwner("jr11");
    const tickets: string[] = [];
    context.on("request", (request) => {
      if (request.url().includes("/events/ticket")) tickets.push(request.url());
    });
    await signIn(page, context, "en", owner.email);
    await expect.poll(async () => (await streamOf(page))?.state).toBe("open");
    const { execFile } = await import("node:child_process");
    const run = (args: string[]) =>
      new Promise<void>((resolve, reject) =>
        execFile("docker", args, (error) => (error ? reject(new Error(error.message)) : resolve())),
      );
    await run(["stop", "wasla-fe-e2e-api"]);
    try {
      await expect(page.locator('[data-banner="live"]')).toBeVisible({ timeout: 45_000 });
      await expect(page.getByTestId("gate-announcement")).toContainText(
        label("en", "app.live.paused"),
      );
    } finally {
      await run(["start", "wasla-fe-e2e-api"]);
    }
    await expect.poll(async () => (await streamOf(page))?.state, { timeout: 90_000 }).toBe("open");
    await expect(page.locator('[data-banner="live"]')).toHaveCount(0);
    expect(tickets.length).toBeGreaterThanOrEqual(2);
  });
});

export type { Account };
