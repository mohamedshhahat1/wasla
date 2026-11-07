/**
 * DS-08: screenshots of every story in en and ar at 390 and 1280 - evidence for the owner's
 * review, written outside the repository, never a baseline (FEQ-11). Runs only when
 * WASLA_SCREENSHOTS_DIR is set.
 */
import { join } from "node:path";

import { test } from "@playwright/test";

import { DIRECTIONS, openStory, storyIds } from "./story";

const out = process.env["WASLA_SCREENSHOTS_DIR"];

test.describe("screenshots", () => {
  test.skip(!out, "WASLA_SCREENSHOTS_DIR not set");
  for (const id of storyIds()) {
    for (const { locale } of DIRECTIONS) {
      for (const width of [390, 1280]) {
        test(`screenshot ${id} [${locale}] ${width}`, async ({ page }) => {
          await page.setViewportSize({ width, height: width === 390 ? 844 : 800 });
          await openStory(page, id, locale);
          await page.waitForTimeout(150);
          await page.screenshot({
            path: join(out!, `${id}.${locale}.${width}.png`),
            fullPage: true,
          });
        });
      }
    }
  }
});
