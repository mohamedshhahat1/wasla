import { describe, expect, it } from "vitest";

import { POST } from "../app/locale/route";
import { publicEnv } from "../lib/env";
import { safeNext } from "../lib/safe-next";

function form(fields: Record<string, string>): Request {
  const body = new URLSearchParams(fields);
  return new Request("http://localhost:3001/locale", {
    method: "POST",
    body,
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
}

describe("the locale switcher's route (AP-04)", () => {
  it("sets wasla_locale only, Lax, Path=/, a year, and redirects back with 303", async () => {
    const response = await POST(form({ locale: "ar", next: "/somewhere" }));
    expect(response.status).toBe(303);
    expect(response.headers.get("Location")).toBe("/somewhere");
    expect(response.headers.get("Set-Cookie")).toBe(
      "wasla_locale=ar; Path=/; Max-Age=31536000; SameSite=Lax",
    );
  });

  it("sets nothing for an unknown locale", async () => {
    const response = await POST(form({ locale: "de", next: "/" }));
    expect(response.headers.get("Set-Cookie")).toBeNull();
  });

  it("never redirects off the app", () => {
    for (const bad of ["https://evil.example/", "//evil.example", "/\\evil.example", "", "x"]) {
      expect(safeNext(bad)).toBe("/");
    }
    expect(safeNext("/a\r\nSet-Cookie: x=1")).toBe("/");
    expect(safeNext("/inbox?x=1")).toBe("/inbox?x=1");
  });
});

describe("the public env", () => {
  it("reads the build-time origins", () => {
    expect(publicEnv()).toEqual({
      environment: "development",
      apiOrigin: "http://localhost:8000",
      appOrigin: "http://localhost:3000",
      consoleOrigin: "http://localhost:3001",
      marketingOrigin: "http://localhost:4321",
    });
  });
});
