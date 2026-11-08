/**
 * Every rule of WSP-03 / WSP-04 fires on its fixture and stays quiet on the clean twin.
 * A rule that cannot fail proves nothing (FE-1 stop condition), so each one is shown failing.
 */
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

import { waslaConfig } from "../eslint/index.js";
import { checkFiles } from "../scripts/check-files.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = join(here, "..", "test-fixtures");
const frontend = resolve(here, "..", "..", "..");

const engines = new Map<string, ESLint>();
function engine(kind: string): ESLint {
  let found = engines.get(kind);
  if (!found) {
    found = new ESLint({
      cwd: frontend,
      overrideConfigFile: true,
      overrideConfig: waslaConfig({ kind, typeChecked: false }) as ESLint.Options["overrideConfig"],
      ignore: false,
    });
    engines.set(kind, found);
  }
  return found;
}

async function ruleIds(kind: string, fixture: string, virtualPath: string): Promise<string[]> {
  const code = readFileSync(join(fixtures, "lint", fixture), "utf8");
  const [result] = await engine(kind).lintText(code, { filePath: join(frontend, virtualPath) });
  expect(result, fixture).toBeDefined();
  const fatal = result!.messages.filter((m) => m.fatal);
  expect(fatal, `${fixture} must parse`).toEqual([]);
  return result!.messages.map((m) => m.ruleId ?? "");
}

const cases: Array<{
  rule: string;
  kind: string;
  path: string;
  bad: string;
  good: string;
  minErrors?: number;
}> = [
  {
    rule: "wasla/no-physical-direction",
    kind: "app-web",
    path: "apps/web/components/box.tsx",
    bad: "physical/bad.tsx",
    good: "physical/good.tsx",
    minErrors: 3,
  },
  {
    rule: "wasla/no-physical-direction",
    kind: "pkg-ui",
    path: "packages/ui/src/box.tsx",
    bad: "physical/bad.tsx",
    good: "physical/good.tsx",
    minErrors: 3,
  },
  {
    rule: "wasla/no-uppercase",
    kind: "app-console",
    path: "apps/console/components/title.tsx",
    bad: "uppercase/bad.tsx",
    good: "uppercase/good.tsx",
    minErrors: 2,
  },
  {
    rule: "wasla/no-public-secret-env",
    kind: "app-web",
    path: "apps/web/lib/env.ts",
    bad: "secret-env/bad.ts",
    good: "secret-env/good.ts",
    minErrors: 2,
  },
  {
    rule: "wasla/no-raw-design-values",
    kind: "app-marketing",
    path: "apps/marketing/src/components/card.tsx",
    bad: "raw-values/bad.tsx",
    good: "raw-values/good.tsx",
    minErrors: 4,
  },
  {
    rule: "wasla/no-web-storage",
    kind: "app-web",
    path: "apps/web/lib/keep.ts",
    bad: "web-storage/bad.ts",
    good: "web-storage/good.ts",
    minErrors: 3,
  },
  {
    rule: "wasla/no-web-storage",
    kind: "pkg-session",
    path: "packages/session/src/preferences-storage.ts",
    bad: "web-storage/allowed-bad.ts",
    good: "web-storage/allowed-good.ts",
    minErrors: 1,
  },
  {
    rule: "wasla/no-server-only-in-client",
    kind: "app-web",
    path: "apps/web/app/session/login/route.ts",
    bad: "server-only/bad.tsx",
    good: "server-only/good.ts",
    minErrors: 1,
  },
  {
    rule: "wasla/console-platform-paths",
    kind: "app-console",
    path: "apps/console/lib/paths.ts",
    bad: "console-paths/bad.ts",
    good: "console-paths/good.ts",
    minErrors: 1,
  },
  {
    rule: "wasla/server-cookies-only-in-session",
    kind: "app-web",
    path: "apps/web/components/profile.ts",
    bad: "server-cookies/bad.ts",
    good: "server-cookies/good.ts",
    minErrors: 2,
  },
  {
    rule: "react/no-danger",
    kind: "app-web",
    path: "apps/web/components/html.tsx",
    bad: "no-danger/bad.tsx",
    good: "no-danger/good.tsx",
    minErrors: 1,
  },
];

describe("workspace lint rules fire on their fixtures", () => {
  for (const c of cases) {
    it(`${c.rule} in ${c.kind}: bad fails, clean twin passes`, async () => {
      const bad = await ruleIds(c.kind, c.bad, c.path);
      expect(bad.filter((id) => id === c.rule).length).toBeGreaterThanOrEqual(c.minErrors ?? 1);
      const good = await ruleIds(c.kind, c.good, c.path);
      expect(good.filter((id) => id === c.rule)).toEqual([]);
    });
  }

  it("a server-only import in a ui file fails even without 'use client'", async () => {
    const ids = await ruleIds("pkg-ui", "server-only/good.ts", "packages/ui/src/leak.ts");
    expect(ids).toContain("wasla/no-server-only-in-client");
  });

  it("cookies are allowed in the session routes and the locale route only (SR-01)", async () => {
    for (const path of [
      "apps/web/app/session/login/route.ts",
      "apps/web/lib/server/session-handlers.ts",
      "apps/web/app/locale/route.ts",
      "apps/console/i18n/request.ts",
    ]) {
      const kind = path.startsWith("apps/console") ? "app-console" : "app-web";
      const ids = await ruleIds(kind, "server-cookies/bad.ts", path);
      expect(ids, path).not.toContain("wasla/server-cookies-only-in-session");
    }
    const elsewhere = await ruleIds(
      "app-console",
      "server-cookies/bad.ts",
      "apps/console/lib/a.ts",
    );
    expect(elsewhere).toContain("wasla/server-cookies-only-in-session");
  });

  it("the console rule does not apply to the customer app", async () => {
    const ids = await ruleIds("app-web", "console-paths/bad.ts", "apps/web/lib/paths.ts");
    expect(ids).not.toContain("wasla/console-platform-paths");
  });
});

describe("import directions (plan section 17) are lint errors", () => {
  const restricted: Array<[string, string, string]> = [
    ["app-marketing", "imports/marketing-session.ts", "apps/marketing/src/a.ts"],
    ["app-marketing", "imports/marketing-api-client.ts", "apps/marketing/src/a.ts"],
    ["app-web", "imports/web-imports-console.ts", "apps/web/lib/a.ts"],
    ["pkg-ui", "imports/package-imports-app.ts", "packages/ui/src/a.ts"],
    ["pkg-i18n", "imports/package-imports-app.ts", "packages/i18n/src/a.ts"],
    ["pkg-ui", "imports/ui-imports-session.ts", "packages/ui/src/a.ts"],
    ["pkg-i18n", "imports/i18n-imports-ui.ts", "packages/i18n/src/a.ts"],
    ["pkg-api-client", "imports/ui-imports-session.ts", "packages/api-client/src/a.ts"],
  ];
  for (const [kind, fixture, path] of restricted) {
    it(`${kind}: ${fixture} is refused`, async () => {
      expect(await ruleIds(kind, fixture, path)).toContain("no-restricted-imports");
    });
  }

  it("the customer app may import session, api-client and ui", async () => {
    const ids = await ruleIds("app-web", "imports/web-allowed.ts", "apps/web/lib/a.ts");
    expect(ids).not.toContain("no-restricted-imports");
  });
});

describe("the file checker (CSS and env files)", () => {
  it("finds physical properties, upper-casing and raw values in an app's CSS", () => {
    const findings = checkFiles("app", [join(fixtures, "css-app", "bad")]);
    expect(findings.some((f) => f.includes("physical margin/padding"))).toBe(true);
    expect(findings.some((f) => f.includes("raw colour"))).toBe(true);
    expect(findings.some((f) => f.includes("uppercase"))).toBe(true);
    expect(checkFiles("app", [join(fixtures, "css-app", "good")])).toEqual([]);
  });

  it("finds physical properties in ui CSS but allows raw values there", () => {
    const findings = checkFiles("ui", [join(fixtures, "css-ui", "bad")]);
    expect(findings.some((f) => f.includes("physical margin/padding"))).toBe(true);
    expect(findings.some((f) => f.includes("text-align"))).toBe(true);
    expect(findings.some((f) => f.includes("raw"))).toBe(false);
  });

  it("finds a secret-looking public variable in an env file", () => {
    expect(checkFiles("app", [join(fixtures, "env", "bad")])).toHaveLength(1);
    expect(checkFiles("app", [join(fixtures, "env", "good")])).toEqual([]);
  });
});
