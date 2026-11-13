/**
 * A test fixture that watches every page (TE-05): each request's origin, CSP violations
 * (the securitypolicyviolation event and the console), and cookies and web storage after the
 * visit (AP-04). A test reads `guard` and asserts.
 */
import { test as base, type Browser, type BrowserContext, type Page } from "@playwright/test";

/**
 * FEF-25: Playwright 1.65 computes accessible names in its own JavaScript world and reads
 * `element.ariaLabelledByElements` there; when axe then reads the same reflected property on the
 * same element from the page's world, Firefox's content process crashes ("Page crashed" -
 * reproduced on Windows and on the Linux runner, bisected to that second read). In Firefox the
 * page's world gets no ARIA element reflection, so axe resolves the same id references through
 * `getAttribute` (its path for engines without reflection) - every rule still runs, with the same
 * references. Chromium and WebKit are untouched.
 */
export async function withoutAriaElementReflection(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    for (const name of Object.getOwnPropertyNames(Element.prototype)) {
      if (/^aria[A-Za-z]*Elements?$/.test(name)) {
        delete (Element.prototype as unknown as Record<string, unknown>)[name];
      }
    }
  });
}

/** A second context for another actor, with the same Firefox rule as the test's own context. */
export async function actorContext(browser: Browser): Promise<BrowserContext> {
  const context = await browser.newContext({ timezoneId: "Africa/Cairo" });
  if (browser.browserType().name() === "firefox") await withoutAriaElementReflection(context);
  return context;
}

export interface Guard {
  origins: Set<string>;
  violations: string[];
  /** Every cookie name in the context, and what both storages hold, right now. */
  storage(page: Page): Promise<{ cookies: string[]; local: number; session: number }>;
}

export const test = base.extend<{ guard: Guard }>({
  context: async ({ context, browserName }, use) => {
    if (browserName === "firefox") await withoutAriaElementReflection(context);
    await use(context);
  },
  guard: async ({ page, context }, use) => {
    const origins = new Set<string>();
    const violations: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.protocol === "http:" || url.protocol === "https:") origins.add(url.origin);
    });
    page.on("console", (message) => {
      const text = message.text();
      if (/Content[- ]Security[- ]Policy|Refused to (load|execute|apply|connect)/i.test(text)) {
        violations.push(text);
      }
    });
    await page.exposeFunction("__waslaCspViolation", (detail: string) => violations.push(detail));
    await page.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", (event) => {
        const report = (window as unknown as { __waslaCspViolation?: (d: string) => void })
          .__waslaCspViolation;
        report?.(`${event.violatedDirective} ${event.blockedURI}`);
      });
    });
    await use({
      origins,
      violations,
      storage: async (current) => {
        const cookies = (await context.cookies()).map((cookie) => cookie.name).sort();
        const sizes = await current.evaluate(() => ({
          local: window.localStorage.length,
          session: window.sessionStorage.length,
        }));
        return { cookies, ...sizes };
      },
    });
  },
});

export { expect } from "@playwright/test";
