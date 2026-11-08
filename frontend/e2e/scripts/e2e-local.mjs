#!/usr/bin/env node
/**
 * pnpm e2e:local (TE-02): builds the three apps, brings FE-0's backend profile up from the
 * repository's docker-compose.e2e.yml (unchanged), waits for health, runs Playwright, and
 * takes the profile down with `down -v` - also when the run fails.
 *
 *   pnpm e2e:local [--skip-build] [-- <playwright args>]
 */
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..", "..");
const repository = resolve(frontend, "..");
const compose = ["compose", "-f", join(repository, "docker-compose.e2e.yml"), "--profile", "e2e"];
const apiPort = process.env["WASLA_E2E_API_PORT"] ?? "8000";

const args = process.argv.slice(2);
const separator = args.indexOf("--");
const own = separator >= 0 ? args.slice(0, separator) : args;
const playwrightArgs = separator >= 0 ? args.slice(separator + 1) : [];

function run(command, commandArgs, options = {}) {
  console.log(`$ ${command} ${commandArgs.join(" ")}`);
  const result = spawnSync(command, commandArgs, {
    stdio: "inherit",
    shell: process.platform === "win32",
    ...options,
  });
  return result.status ?? 1;
}

async function waitForHealth(url, seconds) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  return false;
}

const env = {
  ...process.env,
  WASLA_ENV: "development",
  NEXT_TELEMETRY_DISABLED: "1",
  ASTRO_TELEMETRY_DISABLED: "1",
  TURBO_TELEMETRY_DISABLED: "1",
  // FE-2: the e2e build shows the Google button (the fake Google answers; SR-07, JT-01).
  PUBLIC_GOOGLE_ENABLED: "true",
};

if (!own.includes("--skip-build")) {
  const built = run("pnpm", ["exec", "turbo", "run", "build", "--filter=./apps/*"], {
    cwd: frontend,
    env,
  });
  if (built !== 0) process.exit(built);
}

let status = 1;
try {
  const up = run("docker", [...compose, "up", "-d", "--build"], { cwd: repository });
  if (up !== 0) throw new Error("the e2e profile did not start");
  if (!(await waitForHealth(`http://127.0.0.1:${apiPort}/health`, 180))) {
    throw new Error("the e2e API did not become healthy");
  }
  status = run("pnpm", ["exec", "playwright", "test", ...playwrightArgs], {
    cwd: join(frontend, "e2e"),
    env,
  });
} catch (error) {
  console.error(String(error));
  status = 1;
} finally {
  run("docker", [...compose, "down", "-v", "--remove-orphans"], { cwd: repository });
}
process.exit(status);
