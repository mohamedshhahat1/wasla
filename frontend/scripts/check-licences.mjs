#!/usr/bin/env node
/**
 * Two rules, both enforced; the check fails on either.
 *
 * 1. FX-05 (FE-2): every DIRECT dependency of every workspace package carries a licence from
 *    FE-1's list.
 * 2. FEF-06 (accepted by the owner on 2026-11-12, ADR-195): EVERY package in the lockfile, with
 *    a stricter list for what can reach a browser. The lists, the data-only CC-BY-4.0 packages
 *    and the exceptions live in `licence-policy.json`. "Shipped" is the production dependency
 *    closure of the three apps (conservative: a server-only package in it still counts);
 *    "build-only" is every other package. A shipped violation is never excepted; a build-only
 *    one is excepted only by an exact name, version and licence, and an exception that no longer
 *    matches the lockfile is an error (LC-01..LC-04).
 *
 *   node scripts/check-licences.mjs            # the workspace
 *   node scripts/check-licences.mjs --json     # machine-readable
 */
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** FE-1's list for direct dependencies (FE1_FOUNDATION.md section 3). */
export const ALLOWED_DIRECT = [
  "MIT",
  "ISC",
  "Apache-2.0",
  "BSD-2-Clause",
  "BSD-3-Clause",
  "0BSD",
  "OFL-1.1",
  "MPL-2.0",
];

/**
 * Reads an SPDX expression into a tree: `{ id, exception? }` leaves and `{ op, left, right }`
 * nodes; AND binds tighter than OR, parentheses group, `WITH` attaches an exception to the
 * licence before it (LC-03). Returns null for anything that is not an expression.
 */
export function parseSpdx(expression) {
  if (typeof expression !== "string") return null;
  const tokens = expression.match(/\(|\)|[^\s()]+/g) ?? [];
  let at = 0;
  const peek = () => tokens[at];
  const next = () => tokens[at++];
  const isWord = (token, word) => typeof token === "string" && token.toUpperCase() === word;
  function primary() {
    const token = next();
    if (token === undefined || token === ")") throw new Error("expected a licence");
    if (token === "(") {
      const inner = or();
      if (next() !== ")") throw new Error("expected )");
      return inner;
    }
    if (isWord(token, "AND") || isWord(token, "OR") || isWord(token, "WITH")) {
      throw new Error(`unexpected ${token}`);
    }
    if (isWord(peek(), "WITH")) {
      next();
      const exception = next();
      if (!exception || exception === "(" || exception === ")") {
        throw new Error("expected an exception");
      }
      return { id: token, exception };
    }
    return { id: token };
  }
  function and() {
    let left = primary();
    while (isWord(peek(), "AND")) {
      next();
      left = { op: "AND", left, right: primary() };
    }
    return left;
  }
  function or() {
    let left = and();
    while (isWord(peek(), "OR")) {
      next();
      left = { op: "OR", left, right: and() };
    }
    return left;
  }
  try {
    if (!tokens.length) return null;
    const tree = or();
    return at === tokens.length ? tree : null;
  } catch {
    return null;
  }
}

/** Every `WITH` exception in a tree (printed; the licence before it decides). */
export function spdxExceptions(tree) {
  if (!tree) return [];
  if (tree.op) return [...spdxExceptions(tree.left), ...spdxExceptions(tree.right)];
  return tree.exception ? [tree.exception] : [];
}

function satisfied(tree, allowed) {
  if (!tree) return false;
  if (tree.op === "OR") return satisfied(tree.left, allowed) || satisfied(tree.right, allowed);
  if (tree.op === "AND") return satisfied(tree.left, allowed) && satisfied(tree.right, allowed);
  return allowed.includes(tree.id);
}

/** True when an SPDX expression is satisfied by allowed licences ("A OR B" needs one, "A AND B" all). */
export function licenceAllowed(expression, allowed = ALLOWED_DIRECT) {
  return satisfied(parseSpdx(expression), allowed);
}

function licenceOf(manifest) {
  if (typeof manifest.license === "string") return manifest.license;
  if (manifest.license && typeof manifest.license.type === "string") return manifest.license.type;
  if (Array.isArray(manifest.licenses))
    return manifest.licenses.map((l) => l.type ?? l).join(" OR ");
  return "UNKNOWN";
}

/**
 * Checks the direct dependencies of the given workspace package directories. `readManifest`
 * resolves a dependency's package.json from a package directory (injected for tests).
 */
export function checkDirect(packageDirs, readManifest) {
  const problems = [];
  const seen = [];
  for (const dir of packageDirs) {
    const own = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    const deps = { ...own.dependencies, ...own.devDependencies, ...own.optionalDependencies };
    for (const [name, range] of Object.entries(deps)) {
      if (String(range).startsWith("workspace:")) continue;
      const manifest = readManifest(dir, name);
      const licence = manifest ? licenceOf(manifest) : "UNRESOLVED";
      seen.push({ package: own.name, dependency: name, licence });
      if (!licenceAllowed(licence)) problems.push({ package: own.name, dependency: name, licence });
    }
  }
  return { problems, seen };
}

export function workspacePackageDirs(root) {
  const dirs = [root];
  for (const group of ["apps", "packages"]) {
    const base = join(root, group);
    if (!existsSync(base)) continue;
    for (const name of readdirSync(base)) {
      if (existsSync(join(base, name, "package.json"))) dirs.push(join(base, name));
    }
  }
  if (existsSync(join(root, "e2e", "package.json"))) dirs.push(join(root, "e2e"));
  return dirs;
}

export function nodeManifestReader(dir, name) {
  try {
    const require = createRequire(join(dir, "package.json"));
    return JSON.parse(readFileSync(require.resolve(`${name}/package.json`), "utf8"));
  } catch {
    // Packages whose exports hide package.json: walk node_modules directly.
    const direct = join(dir, "node_modules", name, "package.json");
    return existsSync(direct) ? JSON.parse(readFileSync(direct, "utf8")) : null;
  }
}

/** The policy file at the workspace root (LC-01). */
export function readPolicy(root) {
  return JSON.parse(readFileSync(join(root, "licence-policy.json"), "utf8"));
}

/**
 * One package set from `pnpm licenses list --json`: licence → [{ name, versions }], flattened to
 * `{ name, version, licence }` rows.
 */
export function rowsFromPnpm(byLicence) {
  const rows = [];
  for (const [licence, packages] of Object.entries(byLicence ?? {})) {
    for (const pkg of packages) {
      for (const version of pkg.versions ?? [pkg.version]) {
        rows.push({ name: pkg.name, version, licence: pkg.license ?? licence });
      }
    }
  }
  return rows;
}

const key = (row) => `${row.name}@${row.version}`;
const unique = (rows) => [...new Map(rows.map((row) => [key(row), row])).values()];

/** Splits every package into shipped and build-only (LC-02): build-only = all − shipped. */
export function splitSets(allRows, shippedRows) {
  const shippedKeys = new Set(shippedRows.map(key));
  return {
    shipped: unique(shippedRows),
    buildOnly: unique(allRows.filter((row) => !shippedKeys.has(key(row)))),
  };
}

function allowedFor(row, set, policy) {
  const list = set === "shipped" ? policy.shipped : policy.buildOnly;
  const data =
    set === "buildOnly" && (policy.ccBy4DataPackages ?? []).includes(row.name) ? ["CC-BY-4.0"] : [];
  // The owner's one decision for the shipped set (ADR-195): named data packages, one licence.
  const shippedData = policy.shippedDataPackages;
  const owner =
    set === "shipped" && shippedData && (shippedData.packages ?? []).includes(row.name)
      ? [shippedData.licence]
      : [];
  return [...list, ...data, ...owner];
}

/**
 * The transitive rule (LC-03, LC-04). Returns the violations, the excepted rows, the stale or
 * forbidden exceptions, and every WITH clause met (printed).
 */
export function checkTransitive({ shipped, buildOnly }, policy) {
  const violations = [];
  const excepted = [];
  const withExceptions = [];
  const exceptions = policy.exceptions ?? [];
  const used = new Set();
  for (const [set, rows] of [
    ["shipped", shipped],
    ["buildOnly", buildOnly],
  ]) {
    for (const row of rows) {
      const tree = parseSpdx(row.licence);
      for (const exception of spdxExceptions(tree)) withExceptions.push({ ...row, exception });
      if (satisfied(tree, allowedFor(row, set, policy))) continue;
      const match = exceptions.findIndex(
        (entry) =>
          entry.set === "buildOnly" &&
          entry.name === row.name &&
          entry.version === row.version &&
          entry.licence === row.licence,
      );
      if (set === "buildOnly" && match >= 0) {
        used.add(match);
        excepted.push({ ...row, set, reason: exceptions[match].reason });
        continue;
      }
      violations.push({ ...row, set });
    }
  }
  const stale = exceptions
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry, index }) => entry.set !== "buildOnly" || !used.has(index))
    .map(({ entry }) => ({
      ...entry,
      problem:
        entry.set !== "buildOnly"
          ? "an exception for a shipped package is never allowed"
          : "no package in the lockfile matches this name, version and licence",
    }));
  return { violations, excepted, stale, withExceptions };
}

/** Counts per licence kind, for the printed tables. */
export function kinds(rows) {
  const counts = {};
  for (const row of rows) counts[row.licence] = (counts[row.licence] ?? 0) + 1;
  return Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
}

function pnpmLicences(root, args) {
  const out = execFileSync("pnpm", ["licenses", "list", "--json", ...args], {
    cwd: root,
    encoding: "utf8",
    shell: process.platform === "win32",
    maxBuffer: 256 * 1024 * 1024,
  });
  return rowsFromPnpm(JSON.parse(out));
}

/**
 * The workspace packages whose production dependencies ship (LC-02): the apps, and every
 * workspace package reachable from them through `dependencies` - never through
 * `devDependencies`, which is how build-time tooling (the lint config) stays build-only.
 */
export function shippedWorkspacePackages(manifests, apps) {
  const byName = new Map(manifests.map((manifest) => [manifest.name, manifest]));
  const found = new Set();
  const visit = (name) => {
    if (found.has(name) || !byName.has(name)) return;
    found.add(name);
    for (const [dependency, range] of Object.entries(byName.get(name).dependencies ?? {})) {
      if (String(range).startsWith("workspace:")) visit(dependency);
    }
  };
  for (const app of apps) visit(app);
  return [...found].sort();
}

/** The two sets of the real workspace, read through pnpm (every package in the lockfile). */
export function collectFromPnpm(root, policy) {
  const all = pnpmLicences(root, ["--recursive"]);
  const manifests = workspacePackageDirs(root).map((dir) =>
    JSON.parse(readFileSync(join(dir, "package.json"), "utf8")),
  );
  const selected = shippedWorkspacePackages(manifests, policy.shippedApps);
  const filters = selected.flatMap((name) => ["--filter", name]);
  const shipped = pnpmLicences(root, [...filters, "--prod"]);
  return { ...splitSets(all, shipped), workspacePackages: selected };
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const { problems, seen } = checkDirect(workspacePackageDirs(root), nodeManifestReader);
  const policy = readPolicy(root);
  const sets = collectFromPnpm(root, policy);
  const transitive = checkTransitive(sets, policy);
  const failed =
    problems.length > 0 || transitive.violations.length > 0 || transitive.stale.length > 0;
  if (process.argv.includes("--json")) {
    console.log(
      JSON.stringify(
        {
          problems,
          seen,
          shipped: { count: sets.shipped.length, kinds: kinds(sets.shipped) },
          buildOnly: { count: sets.buildOnly.length, kinds: kinds(sets.buildOnly) },
          ...transitive,
        },
        null,
        2,
      ),
    );
  } else {
    console.log(
      `direct dependencies checked: ${seen.length}; outside the list: ${problems.length}`,
    );
    for (const p of problems) console.log(`  ${p.package} -> ${p.dependency}: ${p.licence}`);
    console.log(`policy: ${policy.acceptedBy}`);
    console.log(`shipped workspace packages: ${sets.workspacePackages.join(", ")}`);
    for (const [label, rows] of [
      ["shipped (production closure of the three apps)", sets.shipped],
      ["build-only (every other package)", sets.buildOnly],
    ]) {
      console.log(`${label}: ${rows.length} packages`);
      for (const [licence, count] of Object.entries(kinds(rows)))
        console.log(`  ${licence}: ${count}`);
    }
    for (const row of transitive.withExceptions)
      console.log(`  WITH exception: ${row.name}@${row.version} ${row.licence}`);
    console.log(`exceptions in force: ${transitive.excepted.length}`);
    for (const row of transitive.excepted)
      console.log(`  ${row.name}@${row.version} ${row.licence} (${row.reason})`);
    console.log(`violations: ${transitive.violations.length}`);
    for (const row of transitive.violations)
      console.log(`  [${row.set}] ${row.name}@${row.version}: ${row.licence}`);
    console.log(`stale or forbidden exceptions: ${transitive.stale.length}`);
    for (const entry of transitive.stale)
      console.log(`  ${entry.name}@${entry.version} ${entry.licence}: ${entry.problem}`);
  }
  process.exit(failed ? 1 : 0);
}
