#!/usr/bin/env node
/**
 * FX-05 (FE-2): every DIRECT dependency of every workspace package must carry a licence from
 * FE-1's list; the check fails otherwise. Transitive licences are printed, not enforced: the
 * policy for them is the owner's (FEF-06, proposed in FE2_AUTH_SESSION.md).
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

/** True when an SPDX expression is satisfied by allowed licences ("A OR B" needs one, "A AND B" all). */
export function licenceAllowed(expression, allowed = ALLOWED_DIRECT) {
  if (typeof expression !== "string" || !expression.trim()) return false;
  const text = expression.replace(/[()]/g, " ").trim();
  if (/\sOR\s/i.test(text))
    return text.split(/\s+OR\s+/i).some((part) => licenceAllowed(part, allowed));
  if (/\sAND\s/i.test(text))
    return text.split(/\s+AND\s+/i).every((part) => licenceAllowed(part, allowed));
  return allowed.includes(text.trim());
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

function transitiveSummary(root) {
  try {
    const out = execFileSync("pnpm", ["licenses", "list", "--json", "--recursive"], {
      cwd: root,
      encoding: "utf8",
      shell: process.platform === "win32",
      maxBuffer: 64 * 1024 * 1024,
    });
    const byLicence = JSON.parse(out);
    return Object.fromEntries(
      Object.entries(byLicence).map(([licence, packages]) => [licence, packages.length]),
    );
  } catch {
    return null;
  }
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const { problems, seen } = checkDirect(workspacePackageDirs(root), nodeManifestReader);
  const transitive = transitiveSummary(root);
  if (process.argv.includes("--json")) {
    console.log(JSON.stringify({ problems, seen, transitive }, null, 2));
  } else {
    console.log(
      `direct dependencies checked: ${seen.length}; outside the list: ${problems.length}`,
    );
    for (const p of problems) console.log(`  ${p.package} -> ${p.dependency}: ${p.licence}`);
    if (transitive) {
      const outside = Object.entries(transitive).filter(([licence]) => !licenceAllowed(licence));
      console.log(
        `transitive licences (reported, not enforced - FEF-06): ${Object.keys(transitive).length} kinds`,
      );
      for (const [licence, count] of outside) console.log(`  ${licence}: ${count}`);
    }
  }
  process.exit(problems.length ? 1 : 0);
}
