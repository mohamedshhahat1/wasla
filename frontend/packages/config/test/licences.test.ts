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
