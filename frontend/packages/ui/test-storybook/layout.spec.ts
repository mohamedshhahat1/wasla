/**
 * CM-03 / RS-03 (FE-RD): AuthLayout puts the brand panel at the inline end - on the right in
 * English, on the left in Arabic - beside the form on a wide screen. Chromium, 1280.
 */
import { expect, test } from "@playwright/test";

import { DIRECTIONS, openStory } from "./story";

for (const { locale, dir } of DIRECTIONS) {
  const side = dir === "rtl" ? "left" : "right";
  test(`AuthLayout ${dir}: the brand panel is at the inline end (the ${side})`, async ({
    page,
  }) => {
    await openStory(page, "composites-layout--auth-screen", locale);
    const form = await page.getByRole("heading", { level: 1 }).boundingBox();
    const panel = await page.locator("[data-brand-panel]").boundingBox();
    if (!form || !panel) throw new Error("no box");
    if (dir === "rtl") expect(panel.x + panel.width).toBeLessThanOrEqual(form.x);
    else expect(panel.x).toBeGreaterThanOrEqual(form.x + form.width);
  });
}
