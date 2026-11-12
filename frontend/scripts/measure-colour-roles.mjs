#!/usr/bin/env node
/**
 * VQ-03 (FE-RD, ADR-195): how a screenshot's visible area splits into the three colour roles.
 *
 * Every pixel is matched to the nearest colour token of tokens.css in OKLab. Pixels nearest a
 * text token, or farther than `DELTA_E` from every token (anti-aliased edges, the CSS dot pattern,
 * icons), go to "text / other". Dominant, secondary, accent and status are shares of the pixels
 * that remain; "text / other" is a share of all pixels. A token colour that several roles share
 * (white is the surface and the text on blue) is counted for the first role in ROLE_ORDER.
 *
 *   node scripts/measure-colour-roles.mjs <png | folder> ...      # a table
 *   node scripts/measure-colour-roles.mjs --json <png | folder>   # machine-readable
 *   node scripts/measure-colour-roles.mjs --check <folder>        # exit 1 outside the bands
 *
 * A file whose name contains "error" is a screen with an error: its status share is not limited.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { PNG } from "pngjs";

const here = dirname(fileURLToPath(import.meta.url));
/** Where tokens.css and tokens.roles.json are read; WASLA_TOKENS_DIR measures another palette. */
const UI = process.env["WASLA_TOKENS_DIR"] ?? join(here, "..", "packages", "ui", "src");

/** OKLab distance under which a pixel counts as that token (≈ a just-visible difference × 2). */
export const DELTA_E = 0.04;

/** The order a colour shared by two roles is counted in. */
export const ROLE_ORDER = ["dominant", "secondary", "accent", "status", "text"];

/** VQ-03: the average over the measured set, per width, and the hard limits per screen. */
export const BANDS = {
  average: {
    dominant: [55, 68],
    secondary: [24, 36],
    accent: [6, 12],
  },
  screen: {
    dominantMin: 45,
    accentMax: 15,
    statusMax: 3,
    textOtherMax: 25,
  },
};

/** Every colour token of tokens.css as #rrggbb, following var(--x) to the scale. */
export function tokenColours(css) {
  // The first definition is the theme's; later ones are scoped overrides (the console's).
  const raw = {};
  for (const m of css.matchAll(/--(color-[a-z0-9-]+):\s*([^;]+);/g)) {
    if (!(m[1] in raw)) raw[m[1]] = m[2].trim();
  }
  const resolveToken = (name, depth = 0) => {
    const value = raw[name];
    const ref = value && /^var\(--([a-z0-9-]+)\)$/.exec(value);
    if (ref && depth < 4) return resolveToken(ref[1], depth + 1);
    return value && /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : null;
  };
  return Object.fromEntries(
    Object.keys(raw)
      .map((name) => [name, resolveToken(name)])
      .filter(([, hex]) => hex),
  );
}

function srgbToLinear(c) {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

/** sRGB 0..255 → OKLab (Björn Ottosson's matrices). */
export function oklab(r, g, b) {
  const [lr, lg, lb] = [srgbToLinear(r), srgbToLinear(g), srgbToLinear(b)];
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

/** The palette the classifier matches against: one entry per distinct colour, first role wins. */
export function buildPalette(css, roles) {
  const colours = tokenColours(css);
  const byHex = new Map();
  for (const role of ROLE_ORDER) {
    for (const name of roles[role] ?? []) {
      const hex = colours[name];
      if (!hex || byHex.has(hex)) continue;
      const n = parseInt(hex.slice(1), 16);
      byHex.set(hex, { hex, role, name, lab: oklab((n >> 16) & 255, (n >> 8) & 255, n & 255) });
    }
  }
  return [...byHex.values()];
}

/** The role of one pixel: a role name, or "other". Cached per colour. */
export function classifier(palette, threshold = DELTA_E) {
  const cache = new Map();
  return (r, g, b) => {
    const key = (r << 16) | (g << 8) | b;
    const known = cache.get(key);
    if (known) return known;
    const [L, A, B] = oklab(r, g, b);
    let best = null;
    let bestD = Infinity;
    for (const entry of palette) {
      const d = Math.hypot(L - entry.lab[0], A - entry.lab[1], B - entry.lab[2]);
      if (d < bestD) {
        bestD = d;
        best = entry;
      }
    }
    const role = best && bestD <= threshold ? best.role : "other";
    cache.set(key, role);
    return role;
  };
}

/** Shares of one decoded image ({ width, height, data } RGBA). */
export function shares(image, classify) {
  const counts = { dominant: 0, secondary: 0, accent: 0, status: 0, text: 0, other: 0 };
  const { data } = image;
  for (let i = 0; i < data.length; i += 4) counts[classify(data[i], data[i + 1], data[i + 2])]++;
  const total = data.length / 4;
  const roleTotal = counts.dominant + counts.secondary + counts.accent + counts.status;
  const pct = (n, of) => (of ? Math.round((n / of) * 1000) / 10 : 0);
  return {
    dominant: pct(counts.dominant, roleTotal),
    secondary: pct(counts.secondary, roleTotal),
    accent: pct(counts.accent, roleTotal),
    status: pct(counts.status, roleTotal),
    textOther: pct(counts.text + counts.other, total),
    pixels: total,
  };
}

/** Hard-limit problems of one screen (VQ-03). */
export function screenProblems(name, s, bands = BANDS) {
  const problems = [];
  if (s.dominant < bands.screen.dominantMin)
    problems.push(`dominant ${s.dominant} < ${bands.screen.dominantMin}`);
  if (s.accent > bands.screen.accentMax)
    problems.push(`accent ${s.accent} > ${bands.screen.accentMax}`);
  if (!/error/i.test(name) && s.status > bands.screen.statusMax)
    problems.push(`status ${s.status} > ${bands.screen.statusMax}`);
  if (s.textOther > bands.screen.textOtherMax)
    problems.push(`text/other ${s.textOther} > ${bands.screen.textOtherMax}`);
  return problems;
}

/** Average shares of a set of screens, and its band problems. */
export function averageProblems(rows, bands = BANDS) {
  const measured = rows.filter((row) => !/error/i.test(row.name));
  const avg = {};
  for (const role of ["dominant", "secondary", "accent", "status", "textOther"]) {
    avg[role] = measured.length
      ? Math.round(
          (measured.reduce((sum, row) => sum + row.shares[role], 0) / measured.length) * 10,
        ) / 10
      : 0;
  }
  const problems = [];
  for (const [role, [lo, hi]] of Object.entries(bands.average)) {
    if (avg[role] < lo || avg[role] > hi)
      problems.push(`average ${role} ${avg[role]} outside ${lo}-${hi}`);
  }
  return { average: avg, problems, count: measured.length };
}

export function loadPalette() {
  const css = readFileSync(join(UI, "tokens.css"), "utf8");
  const roles = JSON.parse(readFileSync(join(UI, "tokens.roles.json"), "utf8"));
  return buildPalette(css, roles);
}

export function measureFile(path, classify) {
  return shares(PNG.sync.read(readFileSync(path)), classify);
}

function pngsIn(target) {
  if (statSync(target).isDirectory()) {
    return readdirSync(target, { recursive: true })
      .map(String)
      .filter((name) => name.endsWith(".png"))
      .map((name) => join(target, name))
      .sort();
  }
  return [target];
}

/** Groups by the width in the file name ("…-1280.png", "…-390.png"); "all" otherwise. */
const widthOf = (name) => /-(\d{3,4})(?:-[a-z]+)?\.png$/.exec(name)?.[1] ?? "all";

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const args = process.argv.slice(2);
  const json = args.includes("--json");
  const check = args.includes("--check");
  const targets = args.filter((arg) => !arg.startsWith("--"));
  const classify = classifier(loadPalette());
  const rows = targets.flatMap(pngsIn).map((path) => {
    const name = basename(path);
    const result = measureFile(path, classify);
    return {
      name,
      path,
      width: widthOf(name),
      shares: result,
      problems: screenProblems(name, result),
    };
  });
  const widths = [...new Set(rows.map((row) => row.width))].sort();
  const averages = Object.fromEntries(
    widths.map((width) => [width, averageProblems(rows.filter((row) => row.width === width))]),
  );
  const failed =
    rows.some((row) => row.problems.length) ||
    Object.values(averages).some((avg) => avg.problems.length);
  if (json) {
    console.log(JSON.stringify({ deltaE: DELTA_E, bands: BANDS, rows, averages }, null, 2));
  } else {
    console.log("screen | dominant | secondary | accent | status | text/other | problems");
    for (const row of rows) {
      const s = row.shares;
      console.log(
        `${row.name} | ${s.dominant} | ${s.secondary} | ${s.accent} | ${s.status} | ${s.textOther} | ${row.problems.join("; ")}`,
      );
    }
    for (const [width, avg] of Object.entries(averages)) {
      const a = avg.average;
      console.log(
        `AVERAGE ${width} (${avg.count} screens) | ${a.dominant} | ${a.secondary} | ${a.accent} | ${a.status} | ${a.textOther} | ${avg.problems.join("; ")}`,
      );
    }
  }
  process.exit(check && failed ? 1 : 0);
}
