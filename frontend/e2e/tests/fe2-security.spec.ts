/**
 * FE-2's security proofs against the real routes and API (JT-06, FX-02, SR-02, SR-06):
 *   - every /session/* route × every refusal, by direct request: refused, and (counted by the
 *     API's own 10/min bucket) no refused request ever reached the API
 *   - the refresh cookie's attributes read from the Set-Cookie header in every browser; the
 *     jar's sameSite asserted where the engine reports it truthfully (FEF-05)
 *   - SameSite=Strict's behaviour: a cross-site form POST from 127.0.0.1 carries no cookie
 *   - the rate limit counted per signed browser address through the routes (FEB-01's S-2 half)
 */
import { expect, test } from "@playwright/test";

import { logInThroughTheApp, testAddress, useAddress, verifiedOwner, WEB } from "../support/fe2";
import { freshEmail } from "../support/helpers";

const VERBS = [
  "login",
  "register-login",
  "refresh",
  "switch",
  "logout",
  "logout-all",
  "google/authorize",
  "google/callback",
];

const good = (address: string): Record<string, string> => ({
  Origin: WEB,
  "Sec-Fetch-Site": "same-origin",
  "X-Wasla-Session": "1",
  "Content-Type": "application/json",
  "X-Wasla-E2E-Client-IP": address,
});

async function post(verb: string, headers: Record<string, string>, body: unknown = {}) {
  const response = await fetch(`${WEB}/session/${verb}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    redirect: "manual",
  });
  const text = await response.text();
  return { status: response.status, text, response };
}

test.describe("direct requests (one project: they exercise the server, not a browser)", () => {
  test.skip(({ browserName, viewport }) => browserName !== "chromium" || viewport?.width !== 1280);

  test("every route refuses cross-origin, header-less, cross-site and non-JSON POSTs, and GET", async () => {
    const address = testAddress();
    const table: string[] = [];
    for (const verb of VERBS) {
      const refusals: Array<[string, Record<string, string>]> = [
        ["cross-origin", { ...good(address), Origin: "http://127.0.0.1:3000" }],
        ["no X-Wasla-Session", (({ "X-Wasla-Session": _, ...rest }) => rest)(good(address))],
        ["Sec-Fetch-Site cross-site", { ...good(address), "Sec-Fetch-Site": "cross-site" }],
        ["form body", { ...good(address), "Content-Type": "application/x-www-form-urlencoded" }],
      ];
      for (const [name, headers] of refusals) {
        const { status, text } = await post(verb, headers, {
          email: freshEmail("csrf"),
          password: "x",
        });
        expect(status, `${verb} ${name}`).toBe(403);
        expect(text).toContain("session_request_refused");
        table.push(`${verb} | ${name} | ${status}`);
      }
      const get = await fetch(`${WEB}/session/${verb}`, { headers: good(address) });
      expect(get.status, `${verb} GET`).toBe(405);
      expect(get.headers.get("allow")).toBe("POST");
      table.push(`${verb} | GET | 405`);
    }
    // 8 routes × 4 refusals were sent under `address`: had any reached the API, its 10/min
    // address bucket would be spent. A real login from the same address is still answered.
    const real = await post("login", good(address), {
      email: freshEmail("after"),
      password: "not-it-0000",
    });
    expect(real.status).toBe(401);
    test.info().annotations.push({ type: "csrf-table", description: table.join("\n") });
  });

  test("the refresh cookie's attributes, from the Set-Cookie header (FX-02)", async () => {
    const owner = await verifiedOwner("cookie");
    const { status, response, text } = await post("login", good(testAddress()), {
      email: owner.email,
      password: owner.password,
    });
    expect(status).toBe(200);
    expect(text).not.toMatch(/refresh_token/);
    const cookies = response.headers.getSetCookie();
    expect(cookies).toHaveLength(1);
    const [pair, ...attributes] = cookies[0]!.split(/;\s*/);
    expect(pair!.split("=")[0]).toBe("wasla_rt");
    expect(attributes).toContain("HttpOnly");
    expect(attributes).toContain("SameSite=Strict");
    expect(attributes).toContain("Path=/");
    expect(attributes).toContain("Max-Age=1209600");
    expect(attributes.some((a) => /^Domain=/i.test(a))).toBe(false);
    // Plain-HTTP development: no Secure (the __Host- name is used everywhere else, SR-03).
    expect(attributes).not.toContain("Secure");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("access-control-allow-origin")).toBeNull();
  });

  test("the rate limit is counted per signed browser address through the routes (SR-06)", async () => {
    const address = testAddress();
    const statuses: number[] = [];
    for (let i = 0; i < 11; i += 1) {
      statuses.push(
        (await post("login", good(address), { email: freshEmail("rl"), password: "not-it-0000" }))
          .status,
      );
    }
    expect(statuses.slice(0, 10)).toEqual(Array(10).fill(401));
    expect(statuses[10]).toBe(429);
    const limited = await post("login", good(address), {
      email: freshEmail("rl"),
      password: "not-it-0000",
    });
    expect(limited.response.headers.get("retry-after")).toMatch(/^\d+$/);
    let other = testAddress();
    while (other === address) other = testAddress();
    expect(
      (await post("login", good(other), { email: freshEmail("rl"), password: "not-it-0000" }))
        .status,
    ).toBe(401);
  });
});

test.describe("in every browser", () => {
  test("the cookie from the login response header and the jar (FX-02, FEF-05)", async ({
    page,
    context,
    browserName,
  }) => {
    const owner = await verifiedOwner("jar");
    await useAddress(context);
    const answer = page.waitForResponse(`${WEB}/session/login`);
    await logInThroughTheApp(page, "en", owner.email);
    const header = (await (await answer).headersArray()).filter(
      (h) => h.name.toLowerCase() === "set-cookie",
    );
    expect(header).toHaveLength(1);
    expect(header[0]!.value).toMatch(/^wasla_rt=[^;]+; .*HttpOnly/);
    expect(header[0]!.value).toContain("SameSite=Strict");
    await page.waitForURL(`${WEB}/`);
    const jar = (await context.cookies(WEB)).filter((c) => c.name === "wasla_rt");
    expect(jar).toHaveLength(1);
    expect(jar[0]!.httpOnly).toBe(true);
    if (browserName === "webkit" && process.platform === "win32") {
      // FEF-05: Windows WebKit reports sameSite "None" for a Strict cookie; the header above and
      // the cross-site test below prove the attribute and its behaviour instead.
      test
        .info()
        .annotations.push({ type: "fef-05", description: `jar sameSite ${jar[0]!.sameSite}` });
    } else {
      expect(jar[0]!.sameSite).toBe("Strict");
    }
    expect(await page.evaluate(() => document.cookie)).not.toContain("wasla_rt");
  });

  test("a cross-site form POST to /session/refresh carries no cookie and is refused", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("xsite");
    await useAddress(context);
    await logInThroughTheApp(page, "en", owner.email);
    await page.waitForURL(`${WEB}/`);
    expect((await context.cookies(WEB)).some((c) => c.name === "wasla_rt")).toBe(true);

    const evil = "http://127.0.0.1:3998";
    await context.route(`${evil}/**`, (route) =>
      route.fulfill({
        contentType: "text/html",
        body: `<!doctype html><form id="f" method="post" action="${WEB}/session/refresh"
          enctype="text/plain"><input name='{"workspace_slug":"x"}' value=""></form>
          <script>document.getElementById("f").submit()</script>`,
      }),
    );
    const sent = page.waitForRequest(
      (r) => r.url() === `${WEB}/session/refresh` && r.method() === "POST",
    );
    const answered = page.waitForResponse(`${WEB}/session/refresh`);
    await page.goto(`${evil}/`);
    const request = await sent;
    const headers = await request.allHeaders();
    expect(headers["cookie"] ?? "").not.toContain("wasla_rt");
    expect((await answered).status()).toBe(403);
    // Still signed in: the refused request changed nothing.
    expect((await context.cookies(WEB)).some((c) => c.name === "wasla_rt")).toBe(true);
  });
});
