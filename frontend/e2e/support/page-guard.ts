/**
 * A test fixture that watches every page (TE-05): each request's origin, CSP violations
 * (the securitypolicyviolation event and the console), and cookies and web storage after the
 * visit (AP-04). A test reads `guard` and asserts.
 */
import { test as base, type Page } from "@playwright/test";

export interface Guard {
  origins: Set<string>;
  violations: string[];
  /** Every cookie name in the context, and what both storages hold, right now. */
  storage(page: Page): Promise<{ cookies: string[]; local: number; session: number }>;
}

export const test = base.extend<{ guard: Guard }>({
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
