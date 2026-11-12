/**
 * WCAG 2.2 SC 1.4.3 / 1.4.11 contrast computed from tokens.css (DS-01). Ratios: text ≥ 4.5:1,
 * focus indicator and boundaries ≥ 3:1. A token change that breaks a pair fails here.
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "src", "tokens.css"), "utf8");

/** A token's #rrggbb value, following `var(--other)` to the scale (PA-01). */
function token(name: string, depth = 0): string {
  const match = new RegExp(`--${name}:\\s*([^;]+);`).exec(css);
  const value = match?.[1]?.trim();
  const reference = value ? /^var\(--([a-z0-9-]+)\)$/.exec(value) : null;
  if (reference?.[1] && depth < 4) return token(reference[1], depth + 1);
  if (!value || !/^#[0-9a-fA-F]{6}$/.test(value)) {
    throw new Error(`token --${name} is not a #rrggbb value`);
  }
  return value;
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

/**
 * PA-06 (FE-RD): every role pair a screen can draw. Text 4.5:1; boundaries, indicators and focus
 * 3:1; a disabled control's label kept at 3:1 though WCAG exempts it (a stage choice).
 */
const rolePairs: Array<[string, string, number]> = [
  ["color-text", "color-canvas", TEXT],
  ["color-text", "color-secondary-subtle", TEXT],
  ["color-text", "color-surface-raised", TEXT],
  ["color-text-muted", "color-canvas", TEXT],
  ["color-text-muted", "color-secondary-subtle", TEXT],
  ["color-primary", "color-canvas", TEXT],
  ["color-primary", "color-secondary-subtle", TEXT],
  ["color-primary-hover", "color-canvas", TEXT],
  ["color-on-primary", "color-primary-pressed", TEXT],
  ["color-on-brand", "color-brand", TEXT],
  ["color-on-brand-muted", "color-brand", TEXT],
  ["color-on-brand", "color-console", TEXT],
  ["color-on-brand-muted", "color-console", TEXT],
  ["color-on-brand", "color-console-hover", TEXT],
  ["color-text", "color-warning-subtle", TEXT],
  ["color-text", "color-info-subtle", TEXT],
  ["color-warning", "color-surface", TEXT],
  ["color-success", "color-surface", TEXT],
  ["color-info", "color-surface", TEXT],
  ["color-focus", "color-canvas", NON_TEXT],
  ["color-focus", "color-brand", NON_TEXT],
  ["color-focus-inverse", "color-brand", NON_TEXT],
  ["color-focus-inverse", "color-console", NON_TEXT],
  ["color-focus-inverse", "color-primary", NON_TEXT],
  ["color-on-brand", "color-primary", TEXT],
  ["color-border-strong", "color-surface", NON_TEXT],
  ["color-border-strong", "color-canvas", NON_TEXT],
  ["color-primary", "color-surface", NON_TEXT],
  ["color-danger", "color-surface-sunken", NON_TEXT],
  ["color-text-muted", "color-surface-sunken", NON_TEXT],
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

  for (const [fg, bg, min] of rolePairs) {
    it(`role pair: ${fg} on ${bg} ≥ ${min}:1`, () => {
      const ratio = contrast(token(fg), token(bg));
      expect(ratio, `${fg} on ${bg} = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(min);
    });
  }

  it("the threshold can fail: a 4.0:1 pair is below the text threshold", () => {
    const ratio = contrast("#767676", "#e6e6e6");
    expect(ratio).toBeGreaterThan(3.5);
    expect(ratio).toBeLessThan(TEXT);
  });

  it("prints the contrast table the report quotes", () => {
    const table = [...pairs, ...rolePairs].map(
      ([fg, bg, min]) => `${fg} on ${bg}: ${contrast(token(fg), token(bg)).toFixed(2)} (>= ${min})`,
    );
    console.log(`contrast table (${table.length} pairs)\n${table.join("\n")}`);
    expect(table).toHaveLength(pairs.length + rolePairs.length);
  });

  it("tokens.css resets Tailwind's own theme, so only tokens are utilities", () => {
    expect(css).toMatch(/@theme static \{\s*--\*: initial;/);
  });
});

/** PA-01 / PA-03: every colour token has exactly one role; outside tokens.css only roles are read. */
describe("colour roles (PA-01, PA-03)", () => {
  const roles = JSON.parse(
    readFileSync(join(here, "..", "src", "tokens.roles.json"), "utf8"),
  ) as Record<string, unknown>;
  const colourTokens = [...css.matchAll(/^\s*--(color-[a-z0-9-]+):/gm)].map((m) => m[1] ?? "");
  const listed = (role: string) => (Array.isArray(roles[role]) ? (roles[role] as string[]) : []);

  it("every colour token has exactly one role", () => {
    const owners = new Map<string, string[]>();
    for (const role of Object.keys(roles)) {
      for (const name of listed(role)) owners.set(name, [...(owners.get(name) ?? []), role]);
    }
    for (const name of colourTokens) expect(owners.get(name), `--${name}`).toHaveLength(1);
    expect([...owners.keys()].sort()).toEqual([...new Set(colourTokens)].sort());
  });

  it("the role colours and the text are blue or white; only status leaves the family", () => {
    const hue = (hex: string): number | null => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
      const max = Math.max(r!, g!, b!);
      const min = Math.min(r!, g!, b!);
      if (max - min < 0.02) return null;
      const d = max - min;
      const h = max === r ? ((g! - b!) / d) % 6 : max === g ? (b! - r!) / d + 2 : (r! - g!) / d + 4;
      return (h * 60 + 360) % 360;
    };
    for (const role of ["dominant", "secondary", "accent", "text", "scale"]) {
      for (const name of listed(role)) {
        if (name === "color-scrim") continue;
        const h = hue(token(name));
        if (h === null) continue;
        expect(h, `--${name}`).toBeGreaterThanOrEqual(205);
        expect(h, `--${name}`).toBeLessThanOrEqual(225);
      }
    }
  });

  it("no component, story or app reads the raw scale", () => {
    const root = join(here, "..", "..", "..");
    const rawScale =
      /--color-(?:blue-\d+|white)\b|\b(?:bg|text|border|ring|outline|fill|stroke|decoration|from|via|to)-(?:blue-\d+|white)\b/;
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (
          ["node_modules", ".next", "dist", ".open-next", "storybook-static", ".turbo"].includes(
            entry.name,
          )
        )
          continue;
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (/\.(tsx?|astro|css|mdx)$/.test(entry.name) && !path.endsWith("tokens.css")) {
          const text = readFileSync(path, "utf8");
          if (rawScale.test(text)) offenders.push(path);
        }
      }
    };
    for (const part of [
      "packages/ui/src",
      "apps/web/app",
      "apps/web/components",
      "apps/console/app",
      "apps/marketing/src",
    ]) {
      walk(join(root, part));
    }
    expect(offenders).toEqual([]);
  });
});
