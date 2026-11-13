/**
 * CM-07 (FE-RD): under prefers-reduced-motion every animated part of the primitives is still -
 * transition 0 s, no animation - and without it the same parts do move (the control: the test can
 * fail). Over the built Storybook, in Chromium.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { openStory } from "./story";

interface Motion {
  animationName: string;
  animationDuration: string;
  transitionDuration: string;
}

async function motionOf(locator: Locator): Promise<Motion> {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      animationName: style.animationName,
      animationDuration: style.animationDuration,
      transitionDuration: style.transitionDuration,
    };
  });
}

const still = (m: Motion) =>
  (m.animationName === "none" ||
    m.animationDuration.split(",").every((d) => parseFloat(d) === 0)) &&
  m.transitionDuration.split(",").every((d) => parseFloat(d) === 0);

/** Each animated part, opened from its story; returns the element whose motion is read. */
const PARTS: Array<{ name: string; open: (page: Page) => Promise<Locator> }> = [
  {
    name: "Dialog",
    open: async (page) => {
      await openStory(page, "primitives-overlays--dialog", "en");
      await page.getByTestId("trigger").click();
      return page.getByRole("dialog");
    },
  },
  {
    name: "Popover",
    open: async (page) => {
      await openStory(page, "primitives-overlays--popover", "en");
      await page.getByTestId("trigger").click();
      return page.getByRole("dialog");
    },
  },
  {
    name: "DropdownMenu",
    open: async (page) => {
      await openStory(page, "primitives-overlays--dropdown-menu", "en");
      await page.getByTestId("trigger").click();
      return page.getByRole("menu");
    },
  },
  {
    name: "Toast",
    open: async (page) => {
      await openStory(page, "primitives-overlays--toast", "en");
      return page.locator("li[data-state=open]").first();
    },
  },
  {
    // FE-3 (UI3-03): the navigation sheet enters from the inline start.
    name: "Navigation sheet",
    open: async (page) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await openStory(page, "composites-app--shell", "en");
      await page.locator("[data-nav-trigger]").click();
      return page.locator("[data-nav-sheet]");
    },
  },
  {
    name: "Button",
    open: async (page) => {
      await openStory(page, "primitives-button--default", "en");
      return page.getByRole("button").first();
    },
  },
];

for (const part of PARTS) {
  test(`reduced motion: ${part.name} is still`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const element = await part.open(page);
    await expect(element).toBeVisible();
    const motion = await motionOf(element);
    expect(still(motion), JSON.stringify(motion)).toBe(true);
  });

  test(`control: without reduced motion ${part.name} moves`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const element = await part.open(page);
    await expect(element).toBeVisible();
    const motion = await motionOf(element);
    expect(still(motion), JSON.stringify(motion)).toBe(false);
  });
}

test("the focus ring never animates", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await openStory(page, "primitives-button--default", "en");
  await page.keyboard.press("Tab");
  const focused = page.locator(":focus-visible");
  await expect(focused).toHaveCount(1);
  const outline = await focused.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      width: style.outlineWidth,
      style: style.outlineStyle,
      transition: style.transitionProperty,
    };
  });
  expect(outline.style).toBe("solid");
  expect(outline.width).toBe("2px");
  expect(outline.transition).not.toMatch(/outline|all/);
});
