#!/usr/bin/env node
/**
 * The checks ESLint cannot make because the files are not JavaScript (WSP-04):
 *   CSS   no physical left/right properties, no text-transform: uppercase, and - in the apps -
 *         no raw colours or sizes (tokens.css in packages/ui is the only place raw values live)
 *   env   no secret-looking name under NEXT_PUBLIC_ / PUBLIC_ in any .env* file
 *
 * Usage: wasla-check-files --kind <app|ui|package|config> <dir> [<dir> ...]
 * Exit code 1 and one line per finding when anything is found.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  ".open-next",
  ".astro",
  ".wrangler",
  ".turbo",
  "dist",
  "storybook-static",
  "playwright-report",
  "test-results",
  "coverage",
  "test-fixtures",
]);

const PHYSICAL_CSS = [
  [
    /(?:^|[\s;{])(?:margin|padding|scroll-margin|scroll-padding)-(?:left|right)\s*:/m,
    "physical margin/padding",
  ],
  [/(?:^|[\s;{])(?:left|right)\s*:/m, "physical left/right"],
  [/(?:^|[\s;{])border-(?:left|right)(?:-[a-z]+)?\s*:/m, "physical border"],
  [/(?:^|[\s;{])border-(?:top|bottom)-(?:left|right)-radius\s*:/m, "physical radius"],
  [/text-align\s*:\s*(?:left|right)\b/m, "text-align left/right"],
  [/(?:^|[\s;{])float\s*:\s*(?:left|right)\b/m, "physical float"],
];
const UPPERCASE_CSS = /text-transform\s*:\s*uppercase/m;
const RAW_CSS = [
  [/#[0-9a-fA-F]{3,8}\b/, "raw colour"],
  [/\b(?:rgba?|hsla?|oklch|lab|lch)\(/, "raw colour function"],
  [/(?<![\w-])(?:[1-9]\d*|\d*\.\d+)(?:px|rem|em|pt)\b/, "raw size"],
];
const SECRET_ENV =
  /^\s*(?:export\s+)?((?:NEXT_PUBLIC_|PUBLIC_)[A-Z0-9_]*(?:SECRET|KEY|TOKEN|PASSWORD)[A-Z0-9_]*)\s*=/m;

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const path = join(dir, name);
    const stat = statSync(path);
    if (stat.isDirectory()) yield* walk(path);
    else yield path;
  }
}

/** Removes comments so a commented example does not count. */
function stripCssComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "");
}

/**
 * @param {string} kind  app | ui | package | config
 * @param {string[]} dirs
 * @returns {string[]} findings, one line each
 */
export function checkFiles(kind, dirs) {
  const findings = [];
  for (const dir of dirs) {
    for (const path of walk(dir)) {
      const name = basename(path);
      const shown = relative(process.cwd(), path).replaceAll("\\", "/");
      if (name.endsWith(".css")) {
        const css = stripCssComments(readFileSync(path, "utf8"));
        for (const [pattern, label] of PHYSICAL_CSS) {
          if (pattern.test(css)) findings.push(`${shown}: ${label} (logical properties only)`);
        }
        if (UPPERCASE_CSS.test(css))
          findings.push(`${shown}: text-transform: uppercase (Turkish casing)`);
        if (kind === "app") {
          for (const [pattern, label] of RAW_CSS) {
            if (pattern.test(css)) findings.push(`${shown}: ${label} (tokens only, DS-01)`);
          }
        }
      }
      if (name.startsWith(".env")) {
        const lines = readFileSync(path, "utf8").split(/\r?\n/);
        for (const line of lines) {
          const match = SECRET_ENV.exec(line);
          if (match) findings.push(`${shown}: public variable ${match[1]} looks secret`);
        }
      }
    }
  }
  return findings;
}

const isMain =
  Boolean(process.argv[1]) &&
  resolve(fileURLToPath(import.meta.url)).toLowerCase() === resolve(process.argv[1]).toLowerCase();
if (isMain) {
  const args = process.argv.slice(2);
  const kindIndex = args.indexOf("--kind");
  const kind = kindIndex >= 0 ? args[kindIndex + 1] : "package";
  const dirs = args.filter((_, i) => i !== kindIndex && i !== kindIndex + 1);
  const findings = checkFiles(kind, dirs.length ? dirs : ["."]);
  for (const finding of findings) console.error(finding);
  if (findings.length) process.exit(1);
}
