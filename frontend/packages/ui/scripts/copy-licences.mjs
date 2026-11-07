#!/usr/bin/env node
/** Copies the fonts' OFL licences into an app's public folder, shipped beside the fonts (DS-02). */
import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const source = join(dirname(fileURLToPath(import.meta.url)), "..", "licenses");
const target = resolve(process.argv[2] ?? "public/licenses");
mkdirSync(target, { recursive: true });
for (const name of readdirSync(source)) copyFileSync(join(source, name), join(target, name));
