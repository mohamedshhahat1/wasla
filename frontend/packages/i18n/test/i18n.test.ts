import { cpSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// @ts-expect-error - a plain .mjs script without types
import { checkCatalogues } from "../scripts/check.mjs";
import {
  collator,
  dirOf,
  errorMessageKey,
  formatDate,
  formatDateTime,
  formatMoney,
  formatRelative,
  LOCALES,
  messagesFor,
  negotiate,
  resolveLocale,
  reviewStatus,
  toLower,
  toUpper,
  withFallback,
} from "../src";

/** Intl separates with no-break spaces; the assertions compare plain text. */
const plain = (text: string) => text.replace(/\s/gu, " ");

const here = dirname(fileURLToPath(import.meta.url));
const messages = join(here, "..", "messages");

type Check = { problems: string[]; stats: { keys: Record<string, number> } };
const check = checkCatalogues as (dir: string) => Check;

function scratchCopy(): string {
  const dir = mkdtempSync(join(tmpdir(), "wasla-i18n-"));
  cpSync(messages, dir, { recursive: true });
  return dir;
}
function edit(dir: string, file: string, change: (json: Record<string, unknown>) => void) {
  const path = join(dir, file);
  const json = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
  change(json);
  writeFileSync(path, JSON.stringify(json));
}

describe("i18n:check (IN-02)", () => {
  it("is green on the committed catalogues, with equal key counts", () => {
    const { problems, stats } = check(messages);
    expect(problems).toEqual([]);
    expect(new Set(Object.values(stats.keys)).size).toBe(1);
  });

  it("fails when a key is removed from tr", () => {
    const dir = scratchCopy();
    edit(dir, "tr/errors.json", (json) => delete json["not_found"]);
    expect(check(dir).problems).toContain("tr/errors.json: missing key not_found");
  });

  it("fails when ar has a key en lacks", () => {
    const dir = scratchCopy();
    edit(dir, "ar/shell.json", (json) => (json["stray"] = "x"));
    expect(check(dir).problems.some((p) => p.includes("extra key stray"))).toBe(true);
  });

  it("fails when an Arabic plural lacks a CLDR category", () => {
    const dir = scratchCopy();
    edit(dir, "ar/common.json", (json) => {
      json["messagesCount"] = "{count, plural, one {رسالة} other {# رسالة}}";
    });
    expect(check(dir).problems.some((p) => p.includes("lacks zero, two, few, many"))).toBe(true);
  });

  it("fails when a Turkish plural uses a category Turkish does not have", () => {
    const dir = scratchCopy();
    edit(dir, "tr/common.json", (json) => {
      json["messagesCount"] = "{count, plural, one {# mesaj} few {# mesaj} other {# mesaj}}";
    });
    expect(check(dir).problems.some((p) => p.includes("does not use: few"))).toBe(true);
  });

  it("fails on invalid ICU and on a changed argument name", () => {
    const dir = scratchCopy();
    edit(dir, "fr/common.json", (json) => {
      json["retryIn"] = "{seconds, plural, one {# seconde}";
      json["messagesCount"] =
        "{n, plural, one {# message} many {# de messages} other {# messages}}";
    });
    const problems = check(dir).problems;
    expect(problems.some((p) => p.includes("retryIn is not valid ICU"))).toBe(true);
    expect(problems.some((p) => p.includes("messagesCount uses arguments"))).toBe(true);
  });
});

describe("runtime fallback", () => {
  it("a key missing in a locale falls back to English, never to the key", () => {
    const merged = withFallback({ a: "ar-a" }, { a: "en-a", b: "en-b", nested: { c: "en-c" } });
    expect(merged).toEqual({ a: "ar-a", b: "en-b", nested: { c: "en-c" } });
  });

  it("every locale has every namespace, and _meta never reaches a screen", () => {
    for (const locale of LOCALES) {
      const tree = messagesFor(locale);
      expect(Object.keys(tree).sort()).toEqual(["auth", "common", "errors", "marketing", "shell"]);
      expect(JSON.stringify(tree)).not.toContain("_meta");
    }
  });

  it("ar, fr and tr are drafts awaiting a native review; en is the reviewed source", () => {
    expect(Object.values(reviewStatus("en"))).toEqual([true, true, true, true, true]);
    for (const locale of ["ar", "fr", "tr"] as const) {
      expect(Object.values(reviewStatus(locale))).toEqual([false, false, false, false, false]);
    }
  });

  it("error codes resolve to their key, unknown codes to the fallback", () => {
    expect(errorMessageKey("rate_limited")).toBe("errors.rate_limited");
    expect(errorMessageKey("network_error")).toBe("errors.network_error");
    expect(errorMessageKey("brand_new_code")).toBe("errors.fallback");
  });
});

describe("locales and negotiation", () => {
  it("Arabic is RTL, the others LTR", () => {
    expect(LOCALES.map(dirOf)).toEqual(["ltr", "rtl", "ltr", "ltr"]);
  });
  it("negotiates by q, region and order", () => {
    expect(negotiate("ar-EG,ar;q=0.9,en;q=0.8")).toBe("ar");
    expect(negotiate("de-DE,fr;q=0.5,en;q=0.4")).toBe("fr");
    expect(negotiate("en;q=0.2,tr;q=0.9")).toBe("tr");
    expect(negotiate("*;q=1, de")).toBe("en");
    expect(negotiate("")).toBe("en");
    expect(negotiate(null)).toBe("en");
    expect(negotiate("ar;q=0")).toBe("en");
  });
  it("the cookie wins when valid and is ignored otherwise", () => {
    expect(resolveLocale("tr", "ar")).toBe("tr");
    expect(resolveLocale("xx", "ar")).toBe("ar");
    expect(resolveLocale(undefined, undefined)).toBe("en");
  });
});

describe("formatters (IN-05), with explicit locales and time zones", () => {
  it("formats decimal strings exactly, never through a float", () => {
    expect(plain(formatMoney("0.10", "EGP", "en"))).toBe("EGP 0.10");
    expect(plain(formatMoney("0.00", "EGP", "en"))).toBe("EGP 0.00");
    expect(plain(formatMoney("1234567.89", "EGP", "en"))).toBe("EGP 1,234,567.89");
    // 2^53 + 1 cannot be held by a float; as a string it survives.
    expect(formatMoney("9007199254740993.00", "USD", "en")).toBe("$9,007,199,254,740,993.00");
    expect(formatMoney("1234567.89", "EGP", "fr")).toMatch(/^1\s234\s567,89\sEGP$/u);
    expect(formatMoney("1234567.89", "EGP", "tr")).toContain("1.234.567,89");
  });

  it("Arabic data uses Latin digits by default and Arabic-Indic digits only when asked", () => {
    const latin = formatMoney("1234.50", "EGP", "ar");
    expect(latin).toMatch(/1[,٬]?234[.٫]50/u);
    expect(latin).not.toMatch(/[٠-٩]/u);
    expect(formatMoney("1234.50", "EGP", "ar", "native")).toMatch(/[٠-٩]/u);
  });

  it("refuses an amount that is not a decimal string", () => {
    expect(() => formatMoney("1e3", "EGP", "en")).toThrow();
    expect(() => formatMoney("12,5", "EGP", "en")).toThrow();
    expect(() => formatMoney("10", "egp", "en")).toThrow();
  });

  it("formats dates in the workspace's zone, not the machine's", () => {
    const iso = "2026-11-07T22:30:00Z";
    expect(formatDate(iso, "en", "Africa/Cairo")).toBe("Nov 8, 2026");
    expect(formatDate(iso, "en", "UTC")).toBe("Nov 7, 2026");
    expect(plain(formatDateTime(iso, "en", "Africa/Cairo"))).toMatch(/^Nov 8, 2026, 12:30\sAM$/u);
    expect(formatDate(iso, "ar", "Africa/Cairo")).not.toMatch(/[٠-٩]/u);
  });

  it("formats relative time against an injected now", () => {
    const now = new Date("2026-11-07T12:00:00Z");
    expect(formatRelative("2026-11-07T11:57:00Z", now, "en")).toBe("3 minutes ago");
    expect(formatRelative("2026-11-09T12:00:00Z", now, "en")).toBe("in 2 days");
    expect(formatRelative("2026-11-06T12:00:00Z", now, "en")).toBe("yesterday");
    expect(formatRelative("2026-11-07T12:00:00Z", now, "fr")).toBe("maintenant");
  });

  it("maps Turkish case by the locale (İ/i, I/ı)", () => {
    expect(toUpper("istanbul", "tr")).toBe("İSTANBUL");
    expect(toLower("IĞDIR", "tr")).toBe("ığdır");
    expect(toUpper("istanbul", "en")).toBe("ISTANBUL");
  });

  it("sorts with the reader's collation", () => {
    const names = ["Zeynep", "Çağla", "Ahmet", "Ömer", "Can"];
    expect([...names].sort(collator("tr").compare)).toEqual([
      "Ahmet",
      "Can",
      "Çağla",
      "Ömer",
      "Zeynep",
    ]);
  });
});
