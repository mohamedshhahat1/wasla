import { describe, expect, it } from "vitest";

import { readOrigins } from "../origins.js";

describe("origins (AP-03): a build with a missing or non-https origin fails", () => {
  it("development uses the localhost table", () => {
    expect(readOrigins({ WASLA_ENV: "development" }).origins).toEqual({
      PUBLIC_API_ORIGIN: "http://localhost:8000",
      PUBLIC_APP_ORIGIN: "http://localhost:3000",
      PUBLIC_CONSOLE_ORIGIN: "http://localhost:3001",
      PUBLIC_MARKETING_ORIGIN: "http://localhost:4321",
    });
  });

  it("production is iwasla.tech (FEQ-04)", () => {
    expect(readOrigins({ WASLA_ENV: "production" }).origins).toEqual({
      PUBLIC_API_ORIGIN: "https://api.iwasla.tech",
      PUBLIC_APP_ORIGIN: "https://app.iwasla.tech",
      PUBLIC_CONSOLE_ORIGIN: "https://console.iwasla.tech",
      PUBLIC_MARKETING_ORIGIN: "https://www.iwasla.tech",
    });
  });

  it("refuses a build without WASLA_ENV", () => {
    expect(() => readOrigins({})).toThrow(/WASLA_ENV/);
  });

  it("refuses a preview build without the workers.dev origins", () => {
    expect(() => readOrigins({ WASLA_ENV: "preview" })).toThrow(/PUBLIC_APP_ORIGIN is missing/);
    expect(
      readOrigins({
        WASLA_ENV: "preview",
        PUBLIC_APP_ORIGIN: "https://wasla-preview-web.example.workers.dev",
        PUBLIC_CONSOLE_ORIGIN: "https://wasla-preview-console.example.workers.dev",
        PUBLIC_MARKETING_ORIGIN: "https://wasla-preview-marketing.example.workers.dev",
      }).origins["PUBLIC_API_ORIGIN"],
    ).toBe("https://api.iwasla.tech");
  });

  it("refuses http outside development and http on a public host in development", () => {
    expect(() =>
      readOrigins({ WASLA_ENV: "production", PUBLIC_API_ORIGIN: "http://api.iwasla.tech" }),
    ).toThrow(/must be https outside development/);
    expect(() =>
      readOrigins({ WASLA_ENV: "development", PUBLIC_API_ORIGIN: "http://api.iwasla.tech" }),
    ).toThrow(/http on localhost/);
  });

  it("refuses a path or a trailing slash", () => {
    expect(() =>
      readOrigins({ WASLA_ENV: "production", PUBLIC_APP_ORIGIN: "https://app.iwasla.tech/" }),
    ).toThrow(/origin only/);
    expect(() =>
      readOrigins({ WASLA_ENV: "production", PUBLIC_APP_ORIGIN: "https://app.iwasla.tech/x" }),
    ).toThrow(/origin only/);
  });
});
