#!/usr/bin/env node
/**
 * FX-01 (FE-2, FEF-04): builds the three Workers' outputs on Linux from any machine with Docker,
 * because OpenNext's build fails on Windows ("Access is denied" on pnpm's symlinks).
 *
 * The worktree's frontend/ is mounted READ-ONLY and copied inside the container without any
 * node_modules (Windows' node_modules never reach Linux); pnpm installs from the frozen lockfile
 * there; OpenNext builds web and console, Astro builds marketing; ONLY the outputs
 * (apps/web/.open-next, apps/console/.open-next, apps/marketing/dist) are copied back. Wrangler
 * and its credentials never enter the container: deploying stays on the host.
 *
 *   node scripts/build-workers-linux.mjs [--env development|preview|production] [--apps web,console,marketing]
 *
 * PUBLIC_* variables set on the host are passed through (they override the origin table).
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** Node 24.15.0 (= .nvmrc) on Debian bookworm, pinned by digest. */
export const BUILD_IMAGE =
  "node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d";

const OUTPUTS = {
  web: { dir: "apps/web/.open-next", command: "pnpm --filter @wasla/web run build:worker" },
  console: {
    dir: "apps/console/.open-next",
    command: "pnpm --filter @wasla/console run build:worker",
  },
  marketing: { dir: "apps/marketing/dist", command: "pnpm --filter @wasla/marketing run build" },
};

function option(args, name, fallback) {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

function listFiles(dir) {
  const out = [];
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const path = join(current, name);
      // lstat: the outputs are copied with links dereferenced; a link that is left is listed, not followed.
      const stat = lstatSync(path);
      if (stat.isDirectory()) walk(path);
      else out.push({ path: relative(dir, path).replaceAll("\\", "/"), bytes: stat.size });
    }
  };
  if (existsSync(dir)) walk(dir);
  return out.sort((a, b) => a.path.localeCompare(b.path));
}

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/** File count, total bytes and the entry's hash of one app's output. */
export function summarise(frontend, app) {
  const dir = join(frontend, OUTPUTS[app].dir);
  const files = listFiles(dir);
  const entry = app === "marketing" ? "index.html" : "worker.js";
  const entryPath = join(dir, entry);
  return {
    app,
    files: files.length,
    bytes: files.reduce((sum, file) => sum + file.bytes, 0),
    entry,
    entrySha256: existsSync(entryPath) ? sha256(entryPath) : null,
  };
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const args = process.argv.slice(2);
  const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const environment = option(args, "--env", "preview");
  const apps = option(args, "--apps", "web,console,marketing").split(",");
  for (const app of apps) if (!OUTPUTS[app]) throw new Error(`unknown app ${app}`);
  const storeVolume =
    process.env["WASLA_WORKERBUILD_STORE_VOLUME"] ?? "wasla-workerbuild-pnpm-store";

  const mounts = ["-v", `${frontend}:/src:ro`, "-v", `${storeVolume}:/pnpm-store`];
  for (const app of apps) {
    const host = join(frontend, OUTPUTS[app].dir);
    rmSync(host, { recursive: true, force: true });
    mkdirSync(host, { recursive: true });
    mounts.push("-v", `${host}:/out/${app}`);
  }
  const env = {
    WASLA_ENV: environment,
    NEXT_TELEMETRY_DISABLED: "1",
    ASTRO_TELEMETRY_DISABLED: "1",
    TURBO_TELEMETRY_DISABLED: "1",
    WRANGLER_SEND_METRICS: "false",
    COREPACK_ENABLE_DOWNLOAD_PROMPT: "0",
    CI: "1",
  };
  for (const [name, value] of Object.entries(process.env)) {
    if (name.startsWith("PUBLIC_") && value) env[name] = value;
  }
  const envArgs = Object.entries(env).flatMap(([name, value]) => ["-e", `${name}=${value}`]);
  const builds = apps.map((app) => OUTPUTS[app].command).join(" && ");
  const copies = apps.map((app) => `cp -RL /work/${OUTPUTS[app].dir}/. /out/${app}/`).join(" && ");
  const script = [
    "set -e",
    "mkdir -p /work",
    "tar -C /src --exclude=node_modules --exclude=.next --exclude=.open-next --exclude=dist --exclude=.turbo --exclude=.wrangler --exclude=storybook-static --exclude=test-results --exclude=playwright-report -cf - . | tar -C /work -xf -",
    "cd /work",
    "corepack enable",
    "pnpm config set store-dir /pnpm-store",
    "pnpm install --frozen-lockfile --reporter=append-only",
    builds,
    copies,
  ].join(" && ");
  const result = spawnSync(
    "docker",
    ["run", "--rm", ...mounts, ...envArgs, "--entrypoint", "bash", BUILD_IMAGE, "-c", script],
    { stdio: "inherit" },
  );
  if (result.status !== 0) process.exit(result.status ?? 1);
  console.log(
    JSON.stringify(
      apps.map((app) => summarise(frontend, app)),
      null,
      2,
    ),
  );
}
