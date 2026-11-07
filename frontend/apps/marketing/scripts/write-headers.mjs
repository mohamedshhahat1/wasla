#!/usr/bin/env node
/**
 * After `astro build`: writes dist/_headers (the plan's section 18 headers and the static CSP,
 * served by Workers static assets) and dist/_redirects ("/" → "/en/"), and REFUSES the build if
 * any page has an inline script, an inline style or a style attribute - the CSP allows neither,
 * so such a page would break silently in the browser.
 * Usage: node scripts/write-headers.mjs <dist>
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { readOrigins } from "@wasla/config/origins";

export function marketingCsp(apiOrigin) {
  return [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${apiOrigin}`,
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}

export function headersFile(environment, apiOrigin) {
  const lines = [
    "/*",
    `  Content-Security-Policy: ${marketingCsp(apiOrigin)}`,
    "  Referrer-Policy: strict-origin-when-cross-origin",
    "  Permissions-Policy: camera=(), microphone=(self), geolocation=()",
    "  X-Content-Type-Options: nosniff",
  ];
  if (environment !== "development") {
    lines.push("  Strict-Transport-Security: max-age=63072000; includeSubDomains");
  }
  return `${lines.join("\n")}\n`;
}

function* htmlFiles(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* htmlFiles(path);
    else if (name.endsWith(".html")) yield path;
  }
}

/** Inline scripts / styles / style attributes in the built pages, one line each. */
export function inlineFindings(dist) {
  const findings = [];
  for (const path of htmlFiles(dist)) {
    const html = readFileSync(path, "utf8");
    const shown = relative(dist, path).replaceAll("\\", "/");
    for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
      if (!/\bsrc=/.test(match[1] ?? "") || (match[2] ?? "").trim()) {
        findings.push(`${shown}: inline <script>`);
      }
    }
    if (/<style\b/i.test(html)) findings.push(`${shown}: inline <style>`);
    if (/\sstyle=/i.test(html)) findings.push(`${shown}: style attribute`);
  }
  return findings;
}

const isMain =
  Boolean(process.argv[1]) &&
  resolve(fileURLToPath(import.meta.url)).toLocaleLowerCase("en") ===
    resolve(process.argv[1]).toLocaleLowerCase("en");
if (isMain) {
  const dist = resolve(process.argv[2] ?? "dist");
  const { environment, origins } = readOrigins(process.env);
  const findings = inlineFindings(dist);
  if (findings.length) {
    for (const finding of findings) console.error(finding);
    console.error("the marketing CSP allows no inline script or style");
    process.exit(1);
  }
  writeFileSync(join(dist, "_headers"), headersFile(environment, origins.PUBLIC_API_ORIGIN));
  writeFileSync(join(dist, "_redirects"), "/ /en/ 302\n");
  console.log(`_headers and _redirects written (${environment})`);
}
