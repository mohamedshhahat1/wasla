/**
 * FE-8 leftovers (MF-04): rules the mutants showed FE-8's own tests did not hold.
 *
 * - M-R16 (CK-01): the idempotency key of a hosted payment lives in the press's closure and dies
 *   with it - no billing module reads or writes the browser's storage.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

describe("a hosted payment stores nothing in the browser (CK-01, M-R16)", () => {
  it("no billing module touches web storage, IndexedDB or cookies", () => {
    const root = join(__dirname, "..");
    const modules = [
      ...files(join(root, "app", "(app)", "billing")),
      ...files(join(root, "components", "billing")),
      ...files(join(root, "lib", "billing")),
    ];
    expect(modules.length).toBeGreaterThan(10);
    for (const path of modules) {
      const text = readFileSync(path, "utf8");
      expect(text, path).not.toMatch(/sessionStorage|localStorage|indexedDB|document\.cookie/);
    }
  });
});
