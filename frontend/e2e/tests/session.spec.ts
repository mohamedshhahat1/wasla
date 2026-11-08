/**
 * SS-02, the browser half of J6: two pages of one origin refresh at the same moment, in a real
 * browser with the real Web Locks and BroadcastChannel, and the refresh endpoint is called once
 * per round. The origin is served by Playwright's router (no server); its refresh endpoint
 * counts. The API half - the real /session/refresh - is FE-2's.
 */
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";
import { buildSync } from "esbuild";

const ORIGIN = "http://localhost:3999";

const bundle = buildSync({
  entryPoints: [
    fileURLToPath(new URL("../../packages/session/src/browser/index.ts", import.meta.url)),
  ],
  bundle: true,
  format: "iife",
  globalName: "WaslaSession",
  platform: "browser",
  write: false,
  target: "es2022",
}).outputFiles[0]!.text;

const PAGE = `<!doctype html><html><head><meta charset="utf-8"><script src="/session.js"></script>
<script>
  const s = WaslaSession.createBrowserSession();
  window.refreshNow = () =>
    s.coordinator.refresh(() => fetch("/refresh", { method: "POST" }).then((r) => r.json()));
  window.tokenNow = () => s.holder.current(Date.now());
</script></head><body>session</body></html>`;

test("two tabs refreshing together call the endpoint once per round (10 rounds)", async ({
  context,
}) => {
  let calls = 0;
  await context.route(`${ORIGIN}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/session.js") {
      return route.fulfill({ contentType: "text/javascript", body: bundle });
    }
    if (path === "/refresh") {
      calls += 1;
      const issued = calls;
      // FE-2: held 250 ms (a slower network) so the second tab always calls while the first
      // refresh is in flight - the race under test; an instant answer let the first refresh
      // finish before the other tab called (G-5, Firefox round 6), a legitimate second refresh.
      await new Promise((resolve) => setTimeout(resolve, 250));
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ access_token: `access-${issued}`, expires_in: 900 }),
      });
    }
    return route.fulfill({ contentType: "text/html", body: PAGE });
  });
  const first = await context.newPage();
  const second = await context.newPage();
  await first.goto(`${ORIGIN}/`);
  await second.goto(`${ORIGIN}/`);
  expect(await first.evaluate(() => typeof navigator.locks?.request)).toBe("function");

  for (let round = 1; round <= 10; round += 1) {
    const [a, b] = await Promise.all([
      first.evaluate(() => (window as unknown as { refreshNow(): Promise<string> }).refreshNow()),
      second.evaluate(() => (window as unknown as { refreshNow(): Promise<string> }).refreshNow()),
    ]);
    expect(calls, `round ${round}`).toBe(round);
    expect(a).toBe(`access-${round}`);
    expect(b).toBe(`access-${round}`);
  }
  test.info().annotations.push({ type: "refresh-calls", description: String(calls) });
});
