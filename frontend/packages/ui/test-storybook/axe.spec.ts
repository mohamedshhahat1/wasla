/** DS-06: axe on every story in en and ar - 0 serious / critical violations. */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { DIRECTIONS, openStory, storyIds } from "./story";

const evidence = process.env["WASLA_EVIDENCE_DIR"];

for (const id of storyIds()) {
  for (const { locale } of DIRECTIONS) {
    test(`axe ${id} [${locale}]`, async ({ page }) => {
      await openStory(page, id, locale);
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"])
        .analyze();
      const counts = { critical: 0, serious: 0, moderate: 0, minor: 0 };
      for (const violation of results.violations) {
        const impact = violation.impact ?? "minor";
        counts[impact] += 1;
      }
      if (evidence) {
        const dir = join(evidence, "axe-storybook");
        mkdirSync(dir, { recursive: true });
        writeFileSync(
          join(dir, `${id}.${locale}.json`),
          JSON.stringify(
            {
              id,
              locale,
              counts,
              violations: results.violations.map((v) => ({
                id: v.id,
                impact: v.impact,
                nodes: v.nodes.length,
                help: v.help,
              })),
            },
            null,
            2,
          ),
        );
      }
      const blocking = results.violations.filter(
        (v) => v.impact === "serious" || v.impact === "critical",
      );
      expect(
        blocking.map(
          (v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`,
        ),
      ).toEqual([]);
    });
  }
}
