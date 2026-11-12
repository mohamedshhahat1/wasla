#!/usr/bin/env node
/**
 * FEF-16 (FE-2): the customer app and the console served by workerd - Cloudflare's runtime, as
 * deployed - from their OpenNext builds (`wrangler dev --local`, nothing leaves the machine). Node
 * (`next start`, every e2e run) defines browser globals workerd lacks: a page that reaches one
 * while rendering on the server answers 500 only here.
 *
 *   node scripts/workers-smoke.mjs [--apps web,console]
 *
 * Needs apps/<app>/.open-next (the Workers build, or scripts/build-workers-linux.mjs on Windows).
 * Checks: every page answers 200 with HTML; GET /session/* is 405; a same-origin POST without
 * SESSION_FORWARD_SECRET is 503 session_unavailable - never a 500, never a stack.
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const apps = (args.includes("--apps") ? args[args.indexOf("--apps") + 1] : "web,console").split(
  ",",
);
// The app origin the build was given (development: the origin table): the routes accept only it.
const appOrigin = process.env["PUBLIC_APP_ORIGIN"] ?? "http://localhost:3000";

const CHECKS = {
  web: {
    port: 8791,
    pages: [
      "/login",
      "/signup",
      "/forgot",
      "/reset-password",
      "/invitations/accept",
      "/verify",
      "/",
    ],
    session: true,
  },
  console: { port: 8792, pages: ["/", "/no-such-page"], session: false },
};

function stop(child) {
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      child.kill("SIGTERM");
    }
  }
}

async function ready(origin, seconds) {
  for (let i = 0; i < seconds; i += 1) {
    try {
      await fetch(`${origin}/`, { redirect: "manual" });
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  return false;
}

const failures = [];
for (const app of apps) {
  const check = CHECKS[app];
  const dir = join(frontend, "apps", app);
  if (!existsSync(join(dir, ".open-next", "worker.js"))) {
    failures.push(`${app}: no .open-next/worker.js - build the Worker first`);
    continue;
  }
  const origin = `http://localhost:${check.port}`;
  const child = spawn(
    "pnpm",
    ["exec", "wrangler", "dev", "--local", "--port", String(check.port), "--log-level", "warn"],
    {
      cwd: dir,
      shell: process.platform === "win32",
      detached: process.platform !== "win32",
      env: { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "";
  child.stdout.on("data", (d) => (output += d));
  child.stderr.on("data", (d) => (output += d));
  try {
    if (!(await ready(origin, 120))) {
      failures.push(`${app}: workerd did not start\n${output.slice(-2000)}`);
      continue;
    }
    for (const path of check.pages) {
      const response = await fetch(`${origin}${path}`, { redirect: "manual" });
      const body = await response.text();
      const expected = path === "/no-such-page" ? 404 : 200;
      const line = `${app} GET ${path} → ${response.status}`;
      console.log(line);
      if (response.status !== expected || !/<html/i.test(body)) failures.push(line);
    }
    if (check.session) {
      for (const verb of ["login", "refresh"]) {
        const get = await fetch(`${origin}/session/${verb}`);
        const post = await fetch(`${origin}/session/${verb}`, {
          method: "POST",
          headers: {
            Origin: appOrigin,
            "Sec-Fetch-Site": "same-origin",
            "X-Wasla-Session": "1",
            "Content-Type": "application/json",
          },
          body: "{}",
        });
        const text = await post.text();
        const line = `${app} /session/${verb}: GET ${get.status}, POST ${post.status} ${/"code":"([a-z_]+)"/.exec(text)?.[1] ?? ""}`;
        console.log(line);
        if (get.status !== 405 || post.status !== 503 || !text.includes("session_unavailable")) {
          failures.push(line);
        }
      }
    }
    if (/ReferenceError|is not defined/.test(output)) {
      failures.push(`${app}: workerd logged an error\n${output.slice(-1500)}`);
    }
  } finally {
    stop(child);
  }
}

if (failures.length) {
  console.error(`workers smoke FAILED:\n${failures.join("\n")}`);
  process.exit(1);
}
console.log("workers smoke: every page 200 on workerd, session routes 405 / 503");
