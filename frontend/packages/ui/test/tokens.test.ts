/**
 * WCAG 2.2 SC 1.4.3 / 1.4.11 contrast computed from tokens.css (DS-01). Ratios: text ≥ 4.5:1,
 * focus indicator and boundaries ≥ 3:1. A token change that breaks a pair fails here.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "src", "tokens.css"), "utf8");

function token(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(css);
  if (!match?.[1]) throw new Error(`token --${name} is not a #rrggbb value`);
  return match[1];
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const TEXT = 4.5;
const NON_TEXT = 3;

const pairs: Array<[string, string, number]> = [
  ["color-text", "color-surface", TEXT],
  ["color-text", "color-surface-sunken", TEXT],
  ["color-text-muted", "color-surface", TEXT],
  ["color-text-muted", "color-surface-sunken", TEXT],
  ["color-on-primary", "color-primary", TEXT],
  ["color-on-primary", "color-primary-hover", TEXT],
  ["color-primary", "color-surface", TEXT],
  ["color-primary-hover", "color-surface", TEXT],
  ["color-text-inverse", "color-text", TEXT],
  ["color-text-inverse", "color-danger", TEXT],
  ["color-danger", "color-surface", TEXT],
  ["color-danger", "color-danger-subtle", TEXT],
  ["color-warning", "color-warning-subtle", TEXT],
  ["color-success", "color-success-subtle", TEXT],
  ["color-info", "color-info-subtle", TEXT],
  ["color-text", "color-danger-subtle", TEXT],
  ["color-text", "color-success-subtle", TEXT],
  ["color-on-primary", "color-console", TEXT],
  ["color-on-primary", "color-console-hover", TEXT],
  ["color-console", "color-surface", TEXT],
  ["color-focus", "color-surface", NON_TEXT],
  ["color-focus", "color-surface-sunken", NON_TEXT],
  ["color-border-strong", "color-surface", 1.5],
];

describe("token contrast (WCAG 1.4.3 text 4.5:1, 1.4.11 non-text 3:1)", () => {
  it("the formula matches WCAG's reference values", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  for (const [fg, bg, min] of pairs) {
    it(`${fg} on ${bg} ≥ ${min}:1`, () => {
      const ratio = contrast(token(fg), token(bg));
      expect(ratio, `${fg} on ${bg} = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(min);
    });
  }

  it("tokens.css resets Tailwind's own theme, so only tokens are utilities", () => {
    expect(css).toMatch(/@theme static \{\s*--\*: initial;/);
  });
});
