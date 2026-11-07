import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  headersFile as headers,
  inlineFindings as findings,
  marketingCsp as csp,
} from "../scripts/write-headers.mjs";

describe("the marketing site's headers (AP-05)", () => {
  it("has the plan's static CSP: own scripts and styles only, the API, no framing", () => {
    expect(csp("https://api.iwasla.tech")).toBe(
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
        "font-src 'self'; connect-src 'self' https://api.iwasla.tech; frame-ancestors 'none'; " +
        "base-uri 'none'; form-action 'self'; object-src 'none'",
    );
  });

  it("adds HSTS outside development only", () => {
    expect(headers("development", "http://localhost:8000")).not.toContain("Strict-Transport");
    expect(headers("production", "https://api.iwasla.tech")).toContain(
      "Strict-Transport-Security: max-age=63072000; includeSubDomains",
    );
  });

  it("refuses a page with an inline script, an inline style or a style attribute", () => {
    const dist = mkdtempSync(join(tmpdir(), "wasla-mkt-"));
    mkdirSync(join(dist, "en"));
    writeFileSync(join(dist, "en", "index.html"), '<p style="color:red">x</p><script>1</script>');
    writeFileSync(join(dist, "ok.html"), '<script type="module" src="/a.js"></script>');
    expect(findings(dist).sort()).toEqual([
      "en/index.html: inline <script>",
      "en/index.html: style attribute",
    ]);
  });
});
