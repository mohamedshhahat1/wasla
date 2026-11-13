/**
 * DS-07 / EXT-FE-07: keyboard and placement of the Radix-based primitives in Arabic (RTL) and
 * English (LTR), over the built Storybook. One test per assertion, so the report's table reads
 * straight from the results: component · direction · assertion · pass/fail.
 */
import { expect, test, type Locator } from "@playwright/test";

import { DIRECTIONS, openStory } from "./story";

async function box(locator: Locator) {
  const b = await locator.boundingBox();
  if (!b) throw new Error("element has no box");
  return b;
}

for (const { locale, dir } of DIRECTIONS) {
  const rtl = dir === "rtl";
  const next = rtl ? "ArrowLeft" : "ArrowRight";
  const previous = rtl ? "ArrowRight" : "ArrowLeft";

  test.describe(`${dir} (${locale})`, () => {
    // ---------------------------------------------------------------- Tabs
    test(`Tabs ${dir}: ArrowRight moves to the ${rtl ? "previous" : "next"} tab`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--tabs-default", locale);
      await page.getByRole("tab", { name: "Second" }).click();
      await page.keyboard.press("ArrowRight");
      await expect(page.getByRole("tab", { name: rtl ? "First" : "Third" })).toBeFocused();
    });
    test(`Tabs ${dir}: ArrowLeft moves to the ${rtl ? "next" : "previous"} tab`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--tabs-default", locale);
      await page.getByRole("tab", { name: "Second" }).click();
      await page.keyboard.press("ArrowLeft");
      await expect(page.getByRole("tab", { name: rtl ? "Third" : "First" })).toBeFocused();
    });
    test(`Tabs ${dir}: Home / End reach the first / last tab and select it`, async ({ page }) => {
      await openStory(page, "primitives-overlays--tabs-default", locale);
      await page.getByRole("tab", { name: "Second" }).click();
      await page.keyboard.press("End");
      await expect(page.getByRole("tab", { name: "Third" })).toBeFocused();
      await expect(page.getByRole("tab", { name: "Third" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.keyboard.press("Home");
      await expect(page.getByRole("tab", { name: "First" })).toBeFocused();
    });
    test(`Tabs ${dir}: the first tab sits at the inline start`, async ({ page }) => {
      await openStory(page, "primitives-overlays--tabs-default", locale);
      const first = await box(page.getByRole("tab", { name: "First" }));
      const third = await box(page.getByRole("tab", { name: "Third" }));
      expect(rtl ? first.x > third.x : first.x < third.x).toBe(true);
    });

    // ---------------------------------------------------------------- RadioGroup
    test(`RadioGroup ${dir}: ArrowRight moves to the ${rtl ? "previous" : "next"} option`, async ({
      page,
    }) => {
      await openStory(page, "primitives-fields--radio-group-default", locale);
      await page.getByRole("radio", { name: "Second" }).click();
      // Radix checks the newly focused radio only while the arrow key is still down (it reads a
      // keydown flag in a deferred focus); a human press lasts tens of ms, Playwright's press
      // releases at once - so the key is held across the assertion.
      await page.keyboard.down("ArrowRight");
      const expected = page.getByRole("radio", { name: rtl ? "First" : "Third" });
      await expect(expected).toBeFocused();
      await expect(expected).toHaveAttribute("aria-checked", "true");
      await page.keyboard.up("ArrowRight");
    });
    test(`RadioGroup ${dir}: ArrowLeft moves to the ${rtl ? "next" : "previous"} option`, async ({
      page,
    }) => {
      await openStory(page, "primitives-fields--radio-group-default", locale);
      await page.getByRole("radio", { name: "Second" }).click();
      await page.keyboard.press("ArrowLeft");
      await expect(page.getByRole("radio", { name: rtl ? "Third" : "First" })).toBeFocused();
    });
    test(`RadioGroup ${dir}: ArrowDown / ArrowUp move next / previous`, async ({ page }) => {
      await openStory(page, "primitives-fields--radio-group-default", locale);
      await page.getByRole("radio", { name: "Second" }).click();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("radio", { name: "Third" })).toBeFocused();
      await page.keyboard.press("ArrowUp");
      await expect(page.getByRole("radio", { name: "Second" })).toBeFocused();
    });
    test(`RadioGroup ${dir}: Home / End reach the first / last option`, async ({ page }) => {
      await openStory(page, "primitives-fields--radio-group-default", locale);
      await page.getByRole("radio", { name: "Second" }).click();
      await page.keyboard.press("End");
      await expect(page.getByRole("radio", { name: "Third" })).toBeFocused();
      await page.keyboard.press("Home");
      await expect(page.getByRole("radio", { name: "First" })).toBeFocused();
    });

    // ---------------------------------------------------------------- DropdownMenu
    test(`DropdownMenu ${dir}: Enter opens on the first item, ArrowDown / ArrowUp move, separator skipped`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--dropdown-menu-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("menuitem", { name: "Alpha" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "Beta" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "More" })).toBeFocused();
      await page.keyboard.press("ArrowUp");
      await expect(page.getByRole("menuitem", { name: "Beta" })).toBeFocused();
    });
    test(`DropdownMenu ${dir}: the submenu opens with ${next} and closes with ${previous}`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--dropdown-menu-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("menuitem", { name: "Alpha" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "Beta" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "More" })).toBeFocused();
      await expect(page.getByRole("menuitem", { name: "More" })).toBeFocused();
      await page.keyboard.press(previous);
      await expect(page.getByRole("menuitem", { name: "Gamma" })).toHaveCount(0);
      await page.keyboard.press(next);
      await expect(page.getByRole("menuitem", { name: "Gamma" })).toBeFocused();
      await page.keyboard.press(previous);
      await expect(page.getByRole("menuitem", { name: "Gamma" })).toHaveCount(0);
      await expect(page.getByRole("menuitem", { name: "More" })).toBeFocused();
    });
    test(`DropdownMenu ${dir}: the submenu opens towards the inline end`, async ({ page }) => {
      await openStory(page, "primitives-overlays--dropdown-menu-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("menuitem", { name: "Alpha" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "Beta" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("menuitem", { name: "More" })).toBeFocused();
      await page.keyboard.press(next);
      const parent = await box(page.getByRole("menu").first());
      const sub = await box(page.getByRole("menuitem", { name: "Gamma" }));
      expect(rtl ? sub.x + sub.width <= parent.x + 1 : sub.x >= parent.x + parent.width - 1).toBe(
        true,
      );
    });
    test(`DropdownMenu ${dir}: Escape closes and returns focus to the trigger`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--dropdown-menu-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("menu")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("menu")).toHaveCount(0);
      await expect(page.getByTestId("trigger")).toBeFocused();
    });

    // ---------------------------------------------------------------- Popover
    test(`Popover ${dir}: side inline-end lands on the ${rtl ? "left" : "right"} of the trigger`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--popover-default", locale);
      await page.getByTestId("trigger").click();
      const trigger = await box(page.getByTestId("trigger"));
      const content = await box(page.getByRole("dialog", { name: "Details" }));
      expect(
        rtl
          ? content.x + content.width <= trigger.x + 1
          : content.x >= trigger.x + trigger.width - 1,
      ).toBe(true);
    });
    test(`Popover ${dir}: opening moves focus into the content; Escape closes and returns it`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--popover-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      const content = page.getByRole("dialog", { name: "Details" });
      await expect(content).toBeVisible();
      const inside = await content.evaluate((node) => node.contains(document.activeElement));
      expect(inside).toBe(true);
      await page.keyboard.press("Escape");
      await expect(content).toHaveCount(0);
      await expect(page.getByTestId("trigger")).toBeFocused();
    });

    // ---------------------------------------------------------------- Select
    test(`Select ${dir}: Enter opens, ArrowDown moves, Enter chooses and focus returns`, async ({
      page,
    }) => {
      await openStory(page, "primitives-fields--select-default", locale);
      const trigger = page.getByRole("combobox", { name: "Choice" });
      await trigger.focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toBeVisible();
      await expect(page.getByRole("option", { name: "First" })).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.getByRole("option", { name: "Second" })).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toHaveCount(0);
      await expect(trigger).toHaveText(/Second/);
      await expect(trigger).toBeFocused();
    });
    test(`Select ${dir}: the list aligns with the trigger's inline start`, async ({ page }) => {
      await openStory(page, "primitives-fields--select-default", locale);
      const trigger = page.getByRole("combobox", { name: "Choice" });
      // Measured before opening: Radix Select hides the rest of the page from the a11y tree.
      const t = await box(trigger);
      await trigger.click();
      const list = await box(page.getByRole("listbox"));
      if (rtl) expect(Math.abs(list.x + list.width - (t.x + t.width))).toBeLessThan(2);
      else expect(Math.abs(list.x - t.x)).toBeLessThan(2);
    });
    test(`Select ${dir}: Escape closes and returns focus to the trigger`, async ({ page }) => {
      await openStory(page, "primitives-fields--select-default", locale);
      const trigger = page.getByRole("combobox", { name: "Choice" });
      await trigger.focus();
      await page.keyboard.press("Enter");
      await page.keyboard.press("Escape");
      await expect(page.getByRole("listbox")).toHaveCount(0);
      await expect(trigger).toBeFocused();
    });

    // ---------------------------------------------------------------- Combobox
    test(`Combobox ${dir}: ArrowDown opens, moves the active option, Enter chooses`, async ({
      page,
    }) => {
      await openStory(page, "primitives-fields--combobox-default", locale);
      const input = page.getByRole("combobox", { name: "City" });
      await input.focus();
      await page.keyboard.press("ArrowDown");
      await expect(input).toHaveAttribute("aria-expanded", "true");
      const first = await input.getAttribute("aria-activedescendant");
      await page.keyboard.press("ArrowDown");
      const second = await input.getAttribute("aria-activedescendant");
      expect(first && second && first !== second).toBe(true);
      await page.keyboard.press("Enter");
      await expect(input).toHaveValue("İzmir");
      await expect(input).toHaveAttribute("aria-expanded", "false");
      await expect(input).toBeFocused();
    });
    test(`Combobox ${dir}: focus stays in the input while the list is open; Escape closes`, async ({
      page,
    }) => {
      await openStory(page, "primitives-fields--combobox-default", locale);
      const input = page.getByRole("combobox", { name: "City" });
      await input.focus();
      await page.keyboard.type("a");
      await expect(page.getByRole("listbox")).toBeVisible();
      await expect(input).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("listbox")).toHaveCount(0);
      await expect(input).toBeFocused();
    });
    test(`Combobox ${dir}: the list aligns with the input's inline start`, async ({ page }) => {
      await openStory(page, "primitives-fields--combobox-default", locale);
      const input = page.getByRole("combobox", { name: "City" });
      await input.click();
      const i = await box(input);
      const list = await box(page.getByRole("listbox"));
      if (rtl) expect(Math.abs(list.x + list.width - (i.x + i.width))).toBeLessThan(12);
      else expect(Math.abs(list.x - i.x)).toBeLessThan(12);
    });

    // ---------------------------------------------------------------- Dialog
    test(`Dialog ${dir}: focus is trapped inside while open`, async ({ page }) => {
      await openStory(page, "primitives-overlays--dialog-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      for (let i = 0; i < 8; i += 1) {
        await page.keyboard.press("Tab");
        expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
      }
      for (let i = 0; i < 4; i += 1) {
        await page.keyboard.press("Shift+Tab");
        expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
      }
    });
    test(`Dialog ${dir}: Escape closes and focus returns to the trigger`, async ({ page }) => {
      await openStory(page, "primitives-overlays--dialog-default", locale);
      await page.getByTestId("trigger").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(page.getByTestId("trigger")).toBeFocused();
    });
    // ---------------------------------------------------------------- FE-3 composites
    test(`SwitcherMenu ${dir}: ArrowDown moves between workspaces; Escape returns focus`, async ({
      page,
    }) => {
      await openStory(page, "composites-app--switcher-open", locale);
      const menu = page.locator("[data-switcher-menu]");
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("menuitemradio")).toHaveCount(2);
      await expect(menu.getByRole("menuitemradio").first()).toHaveAttribute("aria-checked", "true");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await expect(menu.getByRole("menuitemradio").nth(1)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(page.locator("[data-switcher-trigger]")).toBeFocused();
    });
    test(`BannerStack ${dir}: the disclosure opens with Enter and says so`, async ({ page }) => {
      await openStory(page, "composites-app--banner-stack-with-more", locale);
      await expect(page.locator("[data-banner]")).toHaveCount(2);
      const more = page.locator("[data-banner-stack] button[aria-expanded]");
      await more.focus();
      await page.keyboard.press("Enter");
      await expect(more).toHaveAttribute("aria-expanded", "true");
      await expect(page.locator("[data-banner]")).toHaveCount(3);
      await page.keyboard.press("Enter");
      await expect(page.locator("[data-banner]")).toHaveCount(2);
    });
    test(`AppShell ${dir}: the side navigation sits at the inline start`, async ({ page }) => {
      await openStory(page, "composites-app--shell", locale);
      const side = await box(page.locator("[data-side-nav]"));
      const main = await box(page.locator("#main"));
      expect(rtl ? side.x > main.x : side.x < main.x).toBe(true);
    });
    test(`AppShell ${dir}: on a phone the sheet opens with Enter, at the inline start; Escape returns focus`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await openStory(page, "composites-app--shell", locale);
      const trigger = page.locator("[data-nav-trigger]");
      await trigger.focus();
      await page.keyboard.press("Enter");
      const sheet = page.locator("[data-nav-sheet]");
      await expect(sheet).toBeVisible();
      await sheet.evaluate((element) =>
        Promise.all(element.getAnimations().map((animation) => animation.finished)),
      );
      const b = await box(sheet);
      if (rtl) expect(Math.round(b.x + b.width)).toBe(390);
      else expect(Math.round(b.x)).toBe(0);
      await expect(sheet.getByRole("link").first()).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(sheet).toHaveCount(0);
      await expect(trigger).toBeFocused();
    });
    test(`Dialog ${dir}: the close button sits at the inline end of the title`, async ({
      page,
    }) => {
      await openStory(page, "primitives-overlays--dialog-default", locale);
      await page.getByTestId("trigger").click();
      const title = await box(page.getByRole("heading", { name: "Rename workspace" }));
      const close = await box(page.locator("[data-dialog-close]"));
      expect(rtl ? close.x < title.x : close.x > title.x).toBe(true);
    });
  });
}
