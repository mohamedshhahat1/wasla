import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/** FX-05: the licence check fails on a direct dependency outside FE-1's list. */
interface LicenceModule {
  licenceAllowed(expression: string): boolean;
  checkDirect(
    dirs: string[],
    read: (dir: string, name: string) => { license?: string } | null,
  ): { problems: Array<{ dependency: string; licence: string }>; seen: unknown[] };
}

const script = new URL("../../../scripts/check-licences.mjs", import.meta.url).href;
const fixture = (name: string) =>
  fileURLToPath(new URL(`../test-fixtures/licences/${name}`, import.meta.url));
const LICENCES: Record<string, string> = {
  "copyleft-lib": "GPL-3.0-only",
  "fine-lib": "MIT",
  "either-lib": "(MIT OR GPL-3.0-only)",
};
const read = (_dir: string, name: string) => ({ license: LICENCES[name] ?? "UNKNOWN" });

describe("check-licences (FX-05)", async () => {
  const mod = (await import(script)) as LicenceModule;

  it("fails on a GPL direct dependency", () => {
    const { problems } = mod.checkDirect([fixture("gpl")], read);
    expect(problems).toEqual([
      expect.objectContaining({ dependency: "copyleft-lib", licence: "GPL-3.0-only" }),
    ]);
  });

  it("passes a clean package; an OR expression needs one allowed licence; workspace deps skipped", () => {
    const { problems, seen } = mod.checkDirect([fixture("clean")], read);
    expect(problems).toEqual([]);
    expect(seen).toHaveLength(2);
  });

  it("reads SPDX expressions", () => {
    expect(mod.licenceAllowed("MIT")).toBe(true);
    expect(mod.licenceAllowed("MIT OR Apache-2.0")).toBe(true);
    expect(mod.licenceAllowed("Apache-2.0 AND LGPL-3.0-or-later")).toBe(false);
    expect(mod.licenceAllowed("UNKNOWN")).toBe(false);
  });
});

/** LC-05: the transitive rule the owner accepted on 2026-11-12 (FEF-06, ADR-195). */
interface Row {
  name: string;
  version: string;
  licence: string;
}
interface Policy {
  shippedDataPackages?: { packages: string[]; licence: string } | undefined;
  shipped: string[];
  buildOnly: string[];
  ccBy4DataPackages: string[];
  shippedApps: string[];
  exceptions: Array<Row & { set: string; reason?: string; path?: string }>;
}
interface TransitiveModule {
  parseSpdx(expression: string): unknown;
  checkTransitive(
    sets: { shipped: Row[]; buildOnly: Row[] },
    policy: Policy,
  ): {
    violations: Array<Row & { set: string }>;
    excepted: Row[];
    stale: Array<{ problem: string }>;
    withExceptions: Array<Row & { exception: string }>;
  };
  readPolicy(root: string): Policy;
  collectFromPnpm(root: string, policy: Policy): { shipped: Row[]; buildOnly: Row[] };
}

describe("check-licences: the transitive policy (LC-01..LC-05)", async () => {
  const mod = (await import(script)) as TransitiveModule;
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const policy = mod.readPolicy(root);
  const row = (name: string, licence: string, version = "1.0.0"): Row => ({
    name,
    version,
    licence,
  });
  const check = (shipped: Row[], buildOnly: Row[], extra: Partial<Policy> = {}) =>
    mod.checkTransitive({ shipped, buildOnly }, { ...policy, ...extra });

  it("the committed policy is the one the owner accepted", () => {
    expect(policy.shipped).toEqual([
      "MIT",
      "ISC",
      "Apache-2.0",
      "BSD-2-Clause",
      "BSD-3-Clause",
      "0BSD",
      "OFL-1.1",
    ]);
    expect([...policy.buildOnly].sort()).toEqual(
      [...policy.shipped, "BlueOak-1.0.0", "CC0-1.0", "Python-2.0", "MPL-2.0"].sort(),
    );
    expect(policy.shippedApps).toEqual(["@wasla/web", "@wasla/console", "@wasla/marketing"]);
  });

  it("fails a GPL package shipped to browsers", () => {
    expect(check([row("copyleft", "GPL-3.0-only")], []).violations).toEqual([
      expect.objectContaining({ name: "copyleft", set: "shipped" }),
    ]);
  });

  it("fails an LGPL package that is only used to build", () => {
    expect(check([], [row("lesser", "LGPL-3.0-only")]).violations).toHaveLength(1);
  });

  it("reads OR, AND, parentheses and WITH", () => {
    expect(check([row("either", "(MIT OR GPL-3.0-only)")], []).violations).toEqual([]);
    expect(check([], [row("both", "(MIT AND Zlib)")]).violations).toHaveLength(1);
    expect(check([], [row("nested", "(MIT AND (Apache-2.0 OR GPL-2.0-only))")]).violations).toEqual(
      [],
    );
    const withClause = check([row("with", "Apache-2.0 WITH LLVM-exception")], []);
    expect(withClause.violations).toEqual([]);
    expect(withClause.withExceptions).toEqual([
      expect.objectContaining({ name: "with", exception: "LLVM-exception" }),
    ]);
    expect(mod.parseSpdx("MIT OR")).toBeNull();
    expect(mod.parseSpdx("(MIT")).toBeNull();
  });

  it("excepts a build-only package only by an exact name, version and licence", () => {
    const exception = {
      name: "both",
      version: "1.0.0",
      licence: "(MIT AND Zlib)",
      set: "buildOnly",
      reason: "test",
      path: "a > both",
    };
    const passed = check([], [row("both", "(MIT AND Zlib)")], { exceptions: [exception] });
    expect(passed.violations).toEqual([]);
    expect(passed.stale).toEqual([]);
    expect(passed.excepted).toHaveLength(1);

    const moved = check([], [row("both", "(MIT AND Zlib)", "1.0.1")], { exceptions: [exception] });
    expect(moved.violations).toHaveLength(1);
    expect(moved.stale).toEqual([
      expect.objectContaining({
        problem: "no package in the lockfile matches this name, version and licence",
      }),
    ]);
  });

  it("never excepts a shipped package", () => {
    const shipped = { ...row("ui-lib", "MPL-2.0"), set: "shipped", reason: "no" };
    const result = check([row("ui-lib", "MPL-2.0")], [], { exceptions: [shipped] });
    expect(result.violations).toHaveLength(1);
    expect(result.stale).toEqual([
      expect.objectContaining({ problem: "an exception for a shipped package is never allowed" }),
    ]);
  });

  it("allows MPL-2.0 to build, never to ship", () => {
    expect(check([], [row("tool", "MPL-2.0")]).violations).toEqual([]);
    expect(check([row("tool", "MPL-2.0")], []).violations).toHaveLength(1);
  });

  it("allows CC-BY-4.0 only on the data packages named in the policy", () => {
    expect(check([], [row("caniuse-lite", "CC-BY-4.0")]).violations).toEqual([]);
    expect(check([], [row("code-lib", "CC-BY-4.0")]).violations).toHaveLength(1);
    expect(check([row("code-lib", "CC-BY-4.0")], []).violations).toHaveLength(1);
  });

  it("ships caniuse-lite only under the owner's named data decision, and only as CC-BY-4.0", () => {
    expect(check([row("caniuse-lite", "CC-BY-4.0")], []).violations).toEqual([]);
    expect(check([row("caniuse-lite", "GPL-3.0-only")], []).violations).toHaveLength(1);
    expect(
      check([row("caniuse-lite", "CC-BY-4.0")], [], { shippedDataPackages: undefined }).violations,
    ).toHaveLength(1);
  });

  it("fails a package with no licence, an unknown one or a custom text", () => {
    for (const licence of ["UNKNOWN", "", "SEE LICENSE IN LICENSE.md", "Custom: proprietary"]) {
      expect(check([], [row("odd", licence)]).violations).toHaveLength(1);
    }
  });

  it("the real lockfile passes", () => {
    const sets = mod.collectFromPnpm(root, policy);
    const result = mod.checkTransitive(sets, policy);
    expect(sets.shipped.length).toBeGreaterThan(0);
    expect(sets.buildOnly.length).toBeGreaterThan(sets.shipped.length);
    expect(result.violations).toEqual([]);
    expect(result.stale).toEqual([]);
  }, 120_000);
});

describe("check-licences: what ships (LC-02)", async () => {
  const mod = (await import(script)) as {
    shippedWorkspacePackages(
      manifests: Array<{ name: string; dependencies?: Record<string, string> }>,
      apps: string[],
    ): string[];
  };

  it("follows workspace links through dependencies, never devDependencies", () => {
    const manifests = [
      { name: "@w/app", dependencies: { "@w/ui": "workspace:*", next: "1.0.0" } },
      { name: "@w/ui", dependencies: { "@w/i18n": "workspace:*" } },
      { name: "@w/i18n" },
      { name: "@w/config", dependencies: { eslint: "9.0.0" } },
      { name: "@w/other", dependencies: { "@w/config": "workspace:*" } },
    ];
    expect(mod.shippedWorkspacePackages(manifests, ["@w/app"])).toEqual([
      "@w/app",
      "@w/i18n",
      "@w/ui",
    ]);
  });
});
