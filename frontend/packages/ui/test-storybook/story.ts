import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

const here = dirname(fileURLToPath(import.meta.url));

export type Locale = "en" | "ar";
export const DIRECTIONS: Array<{ locale: Locale; dir: "ltr" | "rtl" }> = [
  { locale: "en", dir: "ltr" },
  { locale: "ar", dir: "rtl" },
];

/** Every story id of the built Storybook. */
export function storyIds(): string[] {
  const index = JSON.parse(
    readFileSync(join(here, "..", "storybook-static", "index.json"), "utf8"),
  ) as { entries: Record<string, { type: string }> };
  return Object.entries(index.entries)
    .filter(([, entry]) => entry.type === "story")
    .map(([id]) => id)
    .sort();
}

/** Opens one story in the preview iframe with the locale (and so the direction) set. */
export async function openStory(page: Page, id: string, locale: Locale): Promise<void> {
  await page.goto(`/iframe.html?id=${id}&viewMode=story&globals=locale:${locale}`);
  await page.locator("#storybook-root > *").first().waitFor();
  await page.waitForFunction(
    (expected) => document.documentElement.dir === expected,
    locale === "ar" ? "rtl" : "ltr",
  );
  await page.evaluate(() => document.fonts.ready);
}
