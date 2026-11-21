/**
 * FEF-47 (FE-8 leftovers, impeccable round 1): a plan's and a meter's amounts are read by people -
 * grouped digits in the screen's locale, and a byte limit in gigabytes, never "53687091200".
 */
import { describe, expect, it } from "vitest";

import { messagesFor } from "@wasla/i18n";

import { BYTES_PER_GIGABYTE, isByteLimit, limitAmount } from "../lib/billing/limits";

describe("an entitlement's amount (FEF-47)", () => {
  it("groups the digits in the screen's locale, in Latin digits", () => {
    expect(limitAmount("messages_per_period", 10000, "en")).toBe("10,000");
    expect(limitAmount("messages_per_period", 25000, "fr")).toMatch(/^25\s000$/u);
    expect(limitAmount("messages_per_period", 50000, "ar")).toMatch(/^50[,٬]000$/u);
    expect(limitAmount("messages_per_period", 50000, "ar")).not.toMatch(/[٠-٩]/u);
  });

  it("states a byte limit in gigabytes", () => {
    expect(isByteLimit("storage_bytes")).toBe(true);
    expect(isByteLimit("team_members")).toBe(false);
    expect(limitAmount("storage_bytes", 50 * BYTES_PER_GIGABYTE, "en")).toBe("50 GB");
    expect(limitAmount("storage_bytes", 0, "en")).toBe("0 GB");
    expect(limitAmount("storage_bytes", 1.5 * BYTES_PER_GIGABYTE, "en")).toBe("1.5 GB");
  });

  it("names storage without a unit the amount already carries", () => {
    for (const locale of ["en", "ar", "fr", "tr"] as const) {
      const billing = (
        messagesFor(locale)["app"] as unknown as {
          billing: { limit: Record<string, string> };
        }
      ).billing;
      expect(billing.limit["storage_bytes"], locale).not.toMatch(/\(/);
    }
  });
});
