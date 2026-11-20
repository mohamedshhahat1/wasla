/**
 * FE-8 leftovers (MF-04): rules the mutants showed FE-8's own tests did not hold.
 *
 * - M-R16 (CK-01): the idempotency key of a hosted payment lives in the press's closure and dies
 *   with it - no billing module reads or writes the browser's storage.
 * - M-R24 (TM8-01): "Change role" is never offered on one's own row - also when the roster read
 *   before a change still shows another role than the session's.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { memberControls } from "../lib/team/rules";

type TenantRole = "tenant_owner" | "tenant_admin" | "member";

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

describe("Change role is never offered on one's own row (TM8-01, M-R24)", () => {
  const roles: TenantRole[] = ["tenant_owner", "tenant_admin", "member"];

  it("whatever role the roster still shows for the viewer", () => {
    for (const viewer of roles) {
      for (const shown of roles) {
        const controls = memberControls(
          { userId: "me", role: viewer },
          { userId: "me", role: shown, status: "active" },
          2,
        );
        expect(controls.changeRole, `${viewer} seeing themself as ${shown}`).toBe(false);
      }
    }
  });
});
