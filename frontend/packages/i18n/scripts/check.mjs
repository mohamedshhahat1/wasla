#!/usr/bin/env node
/**
 * i18n:check (IN-02). For the catalogue folder given:
 *   - every key of en exists in ar, fr and tr, and no locale has a key en lacks
 *   - every message parses as ICU MessageFormat
 *   - every plural uses exactly its locale's CLDR categories (others allowed only as =N):
 *       ar zero one two few many other · fr one many other · tr one other · en one other
 *     The table is the plan's (section 20) and is cross-checked here against the CLDR data
 *     of this Node's ICU (Intl.PluralRules) - EXT-FE-08 records the versions.
 *   - the same arguments appear in every locale's version of a message
 * Usage: node scripts/check.mjs <messages-dir>   (exit 1 with one line per problem)
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { parse, TYPE } from "@formatjs/icu-messageformat-parser";

export const LOCALES = ["en", "ar", "fr", "tr"];
export const PLURAL_CATEGORIES = {
  en: ["one", "other"],
  ar: ["zero", "one", "two", "few", "many", "other"],
  fr: ["one", "many", "other"],
  tr: ["one", "other"],
};

function flatten(tree, prefix = "", out = {}) {
  for (const [key, value] of Object.entries(tree)) {
    if (key === "_meta") continue;
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out[path] = value;
    else if (value && typeof value === "object") flatten(value, path, out);
    else out[path] = value;
  }
  return out;
}

/** Collects plural option sets and argument names from a parsed message. */
function inspect(elements, plurals = [], args = new Set()) {
  for (const element of elements) {
    if ("value" in element && element.type !== TYPE.literal && element.type !== TYPE.pound) {
      args.add(element.value);
    }
    if (element.type === TYPE.plural) {
      plurals.push({ argument: element.value, options: Object.keys(element.options) });
    }
    if (element.type === TYPE.plural || element.type === TYPE.select) {
      for (const option of Object.values(element.options)) inspect(option.value, plurals, args);
    }
    if (element.type === TYPE.tag) inspect(element.children, plurals, args);
  }
  return { plurals, args };
}

/** @returns {{ problems: string[], stats: Record<string, unknown> }} */
export function checkCatalogues(dir) {
  const problems = [];
  const stats = { keys: {}, plurals: {}, cldr: {} };
  for (const locale of LOCALES) {
    const fromIcu = new Intl.PluralRules(locale).resolvedOptions().pluralCategories;
    stats.cldr[locale] = fromIcu.join(",");
    const expected = [...PLURAL_CATEGORIES[locale]].sort().join(",");
    if ([...fromIcu].sort().join(",") !== expected) {
      problems.push(
        `${locale}: the plan's plural categories (${expected}) differ from CLDR (${fromIcu})`,
      );
    }
  }
  const namespaces = readdirSync(join(dir, "en"))
    .filter((name) => name.endsWith(".json"))
    .sort();
  const read = (locale, file) => {
    try {
      return flatten(JSON.parse(readFileSync(join(dir, locale, file), "utf8")));
    } catch (error) {
      problems.push(`${locale}/${file}: cannot be read (${error.message})`);
      return {};
    }
  };
  for (const file of namespaces) {
    const source = read("en", file);
    const sourceArgs = {};
    for (const locale of LOCALES) {
      const messages = locale === "en" ? source : read(locale, file);
      stats.keys[locale] = (stats.keys[locale] ?? 0) + Object.keys(messages).length;
      if (locale !== "en") {
        for (const key of Object.keys(source)) {
          if (!(key in messages)) problems.push(`${locale}/${file}: missing key ${key}`);
        }
        for (const key of Object.keys(messages)) {
          if (!(key in source)) problems.push(`${locale}/${file}: extra key ${key} (not in en)`);
        }
      }
      for (const [key, text] of Object.entries(messages)) {
        if (typeof text !== "string" || text.trim() === "") {
          problems.push(`${locale}/${file}: ${key} is empty or not a string`);
          continue;
        }
        let ast;
        try {
          ast = parse(text, { requiresOtherClause: true });
        } catch (error) {
          problems.push(`${locale}/${file}: ${key} is not valid ICU (${error.message})`);
          continue;
        }
        const { plurals, args } = inspect(ast);
        if (locale === "en") sourceArgs[key] = [...args].sort().join(",");
        else if (key in sourceArgs && sourceArgs[key] !== [...args].sort().join(",")) {
          problems.push(
            `${locale}/${file}: ${key} uses arguments {${[...args]}} but en uses {${sourceArgs[key]}}`,
          );
        }
        for (const plural of plurals) {
          stats.plurals[locale] = (stats.plurals[locale] ?? 0) + 1;
          const categories = plural.options.filter((option) => !option.startsWith("="));
          const required = PLURAL_CATEGORIES[locale];
          const missing = required.filter((category) => !categories.includes(category));
          const unknown = categories.filter((category) => !required.includes(category));
          if (missing.length) {
            problems.push(
              `${locale}/${file}: ${key} plural {${plural.argument}} lacks ${missing.join(", ")}`,
            );
          }
          if (unknown.length) {
            problems.push(
              `${locale}/${file}: ${key} plural {${plural.argument}} has categories ${locale} does not use: ${unknown.join(", ")}`,
            );
          }
        }
      }
    }
  }
  return { problems, stats };
}

const isMain =
  Boolean(process.argv[1]) &&
  resolve(fileURLToPath(import.meta.url)).toLocaleLowerCase("en") ===
    resolve(process.argv[1]).toLocaleLowerCase("en");
if (isMain) {
  const dir = resolve(process.argv[2] ?? "messages");
  const { problems, stats } = checkCatalogues(dir);
  for (const problem of problems) console.error(problem);
  console.log(
    `i18n:check ${problems.length ? "FAILED" : "ok"} - keys ${JSON.stringify(stats.keys)}, plurals ${JSON.stringify(stats.plurals)}, CLDR (ICU ${process.versions.icu}, CLDR ${process.versions.cldr}) ${JSON.stringify(stats.cldr)}`,
  );
  if (problems.length) process.exit(1);
}
