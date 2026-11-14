/**
 * FE-2's journey helpers (JT-01..JT-08): a signed address per test, accounts made through the
 * real API, the locale's own labels, and the "nothing reachable" proof run after every step.
 */
import type { BrowserContext, Page } from "@playwright/test";

import { messagesFor, type Locale } from "@wasla/i18n";

import { API_ORIGIN, APPS } from "./env";
import { freshEmail, mailbox, signedForward } from "./helpers";

export const WEB = APPS.web.origin;
export const PASSWORD = "Correct-Horse-Battery-9";
export const REFRESH_COOKIES = ["wasla_rt", "__Host-wasla_rt"];
/**
 * The verification mail's subject (app/services/email_templates.py, a constant). Codes are read
 * from these mails only: the mailbox lists every six-digit run of any mail as a code, and an
 * invitation's random token or slug can hold one (FEB-32, frontend.yml 43576090907).
 */
export const VERIFICATION_SUBJECT = "Verify your email address for Wasla";

async function verificationMails(email: string) {
  return (await mailbox.messagesTo(email)).filter(
    (message) => message.subject === VERIFICATION_SUBJECT,
  );
}

/** A documentation-range address unique to this test, so tests never share a rate-limit bucket. */
export function testAddress(): string {
  const a = Math.floor(Math.random() * 250) + 1;
  const b = Math.floor(Math.random() * 250) + 1;
  return `198.19.${a}.${b}`;
}

/**
 * Every /session/* call of this page is signed by the route handler under `address` (the e2e
 * test header, honoured only with WASLA_E2E_ROUTES=1 on a development build - SR-04).
 */
export async function useAddress(
  context: BrowserContext,
  address = testAddress(),
): Promise<string> {
  await context.route(`${WEB}/session/**`, async (route) => {
    const headers = { ...route.request().headers(), "x-wasla-e2e-client-ip": address };
    await route.continue({ headers });
  });
  return address;
}

/** The visitor's language, as the language switcher would set it. */
export async function useLocale(context: BrowserContext, locale: Locale): Promise<void> {
  await context.addCookies([{ name: "wasla_locale", value: locale, url: WEB }]);
}

/** One catalogue string of `locale` (labels are found by the locale's own text). */
export function label(locale: Locale, key: string): string {
  let node: unknown = messagesFor(locale);
  for (const part of key.split(".")) node = (node as Record<string, unknown>)[part];
  if (typeof node !== "string") throw new Error(`no catalogue string ${locale}:${key}`);
  return node;
}

/**
 * A form field by its label in `locale`: the label's text at the start of the accessible name
 * (FormField appends the "required" mark), so "New password" never matches "Repeat the new password".
 */
export function field(page: Page, locale: Locale, key: string) {
  const text = label(locale, key).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const name = new RegExp(`^${text}(\\s|$)`);
  return page.getByRole("textbox", { name }).or(page.getByRole("combobox", { name }));
}

/** The plain-text part of a rich string (before its first tag). */
export function lead(locale: Locale, key: string): string {
  return label(locale, key).split("<")[0]!.trim();
}

/** A call to the e2e API from the test runner, signed under `address`. */
export async function api(
  method: "GET" | "POST",
  path: string,
  options: { body?: unknown; token?: string; address?: string } = {},
): Promise<{ status: number; json: unknown }> {
  const headers: Record<string, string> = {
    ...signedForward(options.address ?? testAddress(), method, path),
  };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.token) headers["Authorization"] = `Bearer ${options.token}`;
  const response = await fetch(`${API_ORIGIN}${path}`, {
    method,
    headers,
    body: options.body === undefined ? null : JSON.stringify(options.body),
  });
  const text = await response.text();
  return { status: response.status, json: text ? (JSON.parse(text) as unknown) : null };
}

/** Waits for the newest code the mailbox holds for `email` (never a fixed delay). */
export async function waitForCode(
  email: string,
  after = new Set<string>(),
  attempts = 60,
): Promise<string> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const messages = await verificationMails(email);
    const code = messages.flatMap((message) => message.codes).find((c) => !after.has(c));
    if (code) return code;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`no code arrived for ${email}`);
}

/** Waits for the newest link to `path` the mailbox holds for `email`. */
export async function waitForLink(email: string, path: string): Promise<string> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const messages = await mailbox.messagesTo(email);
    const link = messages.flatMap((message) => message.links).find((l) => l.includes(path));
    if (link) return link;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`no ${path} link arrived for ${email}`);
}

export interface Account {
  email: string;
  password: string;
  slug: string;
  token: string;
  address: string;
}

/** A verified owner with one workspace, made through the API (register, log in, verify). */
export async function verifiedOwner(prefix = "own"): Promise<Account> {
  const address = testAddress();
  const email = freshEmail(prefix);
  const slug = `ws-${Math.random().toString(36).slice(2, 10)}`;
  const registered = await api("POST", "/api/v1/auth/register", {
    address,
    body: {
      email,
      password: PASSWORD,
      full_name: "E2E Owner",
      workspace_name: `Workspace ${slug}`,
      workspace_slug: slug,
      timezone: "Africa/Cairo",
    },
  });
  if (registered.status !== 202) throw new Error(`register answered ${registered.status}`);
  const login = await api("POST", "/api/v1/auth/login", {
    address,
    body: { email, password: PASSWORD },
  });
  const token = (login.json as { access_token: string }).access_token;
  // Register already mailed the first code (AuthService._request_email_verification).
  const code = await waitForCode(email);
  const verified = await api("POST", "/api/v1/auth/email/verification/verify", {
    address,
    token,
    body: { code },
  });
  if (verified.status !== 200) throw new Error(`verify answered ${verified.status}`);
  return { email, password: PASSWORD, slug, token, address };
}

/** Logs in through the app's own login screen (the route handler sets the cookie). */
export async function logInThroughTheApp(page: Page, locale: Locale, email: string): Promise<void> {
  await page.goto(`${WEB}/login`);
  await field(page, locale, "auth.email").fill(email);
  await field(page, locale, "auth.password").fill(PASSWORD);
  await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
}

/**
 * JT-06 after every step: the refresh cookie is not readable by page script, no token is in
 * any storage, URL or history entry, and IndexedDB holds no database.
 */
export async function nothingReachable(page: Page, context: BrowserContext): Promise<void> {
  const seen = await page.evaluate(async () => ({
    cookie: document.cookie,
    local: window.localStorage.length,
    session: window.sessionStorage.length,
    databases: (await (indexedDB.databases?.() ?? Promise.resolve([]))).length,
    url: window.location.href,
    history: JSON.stringify(window.history.state ?? null),
  }));
  for (const name of REFRESH_COOKIES) {
    if (seen.cookie.includes(`${name}=`)) throw new Error(`${name} is readable by page script`);
  }
  if (seen.local || seen.session) throw new Error("web storage is not empty");
  if (seen.databases) throw new Error("IndexedDB holds a database");
  if (/eyJ[A-Za-z0-9_-]{10,}/.test(seen.url + seen.history)) {
    throw new Error("a token is in the URL or the history entry");
  }
  const jar = await context.cookies(WEB);
  for (const cookie of jar.filter((c) => REFRESH_COOKIES.includes(c.name))) {
    if (!cookie.httpOnly) throw new Error(`${cookie.name} is not HttpOnly`);
  }
}

/**
 * Presses "Send a new code" until the API refuses with 429 (3 sends per 15 minutes,
 * EmailVerificationService): the screen then shows the countdown it was told. Returns how many
 * sends the API accepted, so a caller can wait for that many new codes.
 */
export async function resendUntilCooldown(page: Page, locale: Locale): Promise<number> {
  let accepted = 0;
  for (let press = 0; press < 5; press += 1) {
    const sent = page.waitForResponse((r) => r.url().endsWith("/auth/email/verification/send"));
    await page.getByRole("button", { name: label(locale, "auth.verify.resend") }).click();
    const response = await sent;
    if (response.status() === 429) return accepted;
    if (response.status() !== 202) throw new Error(`send answered ${response.status()}`);
    accepted += 1;
  }
  throw new Error("the API never refused a resend");
}

/** The newest code once the mailbox holds `count` messages for `email`. */
export async function newestCode(email: string, count: number): Promise<string> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const messages = await verificationMails(email);
    if (messages.length >= count && messages[0]?.codes[0]) return messages[0].codes[0];
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`fewer than ${count} messages arrived for ${email}`);
}
