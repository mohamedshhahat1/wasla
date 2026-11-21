/**
 * ADR-204's pure rules in the browser: the origin Meta's events may come from (SD-05), the session
 * info read in its v5 and v4 shapes (ST-02), CANCEL's steps and ERROR's sentences (ST-04, EM-01),
 * the code's 5 minutes (ST-09), FB.login's extras (SD-04), the adapter order (UA-01 / UA-02),
 * home's warnings (HM9), the one route's CSP (SD-06) and the stream's two new frames (OS-05).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { EVENT_TYPES } from "@wasla/api-client";
import { buildCsp } from "@wasla/session/headers";

import { availableAdapters, CONNECT_ADAPTERS } from "../lib/channels";
import { coexistenceWarnings, disconnectReasonKey, DISCONNECT_REASONS } from "../lib/coexistence";
import {
  CANCEL_STEPS,
  cancelStepKey,
  codeStillFresh,
  ERROR_SENTENCES,
  errorSentenceKey,
  loginExtras,
  META_SDK_URL,
  readSessionEvent,
  trustedOrigin,
  type SdkSource,
} from "../lib/embedded-signup";
import { EMBEDDED_SIGNUP_ROUTE, embeddedSignupCsp } from "../proxy";

const here = dirname(fileURLToPath(import.meta.url));
const META: SdkSource = { url: META_SDK_URL, origin: null };
const FAKE: SdkSource = {
  url: "http://127.0.0.1:4010/meta/sdk.js",
  origin: "http://127.0.0.1:4010",
};

const en = JSON.parse(
  readFileSync(join(here, "../../../packages/i18n/messages/en/app.json"), "utf8"),
) as { embedded: { steps: Record<string, string>; errors: Record<string, string> } } & Record<
  string,
  unknown
>;

function event(type: string, body: Record<string, unknown>) {
  return JSON.stringify({ type: "WA_EMBEDDED_SIGNUP", event: type, data: body });
}

describe("SD-05: only Meta's origin speaks", () => {
  it.each([
    ["https://www.facebook.com", true],
    ["https://facebook.com", true],
    ["https://business.facebook.com", true],
    ["https://evilfacebook.com", false],
    ["https://facebook.com.evil.test", false],
    ["http://www.facebook.com", false],
    ["null", false],
    ["http://127.0.0.1:4010", false],
  ])("%s → %s", (origin, trusted) => {
    expect(trustedOrigin(origin, META)).toBe(trusted);
  });

  it("the e2e build trusts the fakes' origin and nothing else, Meta's included", () => {
    expect(trustedOrigin("http://127.0.0.1:4010", FAKE)).toBe(true);
    expect(trustedOrigin("https://www.facebook.com", FAKE)).toBe(false);
  });

  it("a message from any other origin is ignored before it is parsed (M-R31)", () => {
    const finish = event("FINISH", { phone_number_id: "1", messaging_account_ids: ["2"] });
    expect(readSessionEvent("https://evil.test", finish, META)).toBeNull();
  });
});

describe("ST-02: session info, v5 and v4", () => {
  it("v5 FINISH reads the first messaging account", () => {
    const data = event("FINISH", {
      phone_number_id: "pn-1",
      messaging_account_ids: ["ma-1"],
      is_on_biz_app: false,
      business_id: "b-1",
    });
    expect(readSessionEvent("https://www.facebook.com", data, META)).toEqual({
      kind: "finish",
      onBusinessApp: false,
      phoneNumberId: "pn-1",
      messagingAccountId: "ma-1",
      businessId: "b-1",
    });
  });

  it("v5 FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING is a Business app number (a hint)", () => {
    const data = event("FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING", {
      phone_number_id: "pn-1",
      messaging_account_ids: ["ma-1"],
    });
    expect(readSessionEvent("https://www.facebook.com", data, META)).toMatchObject({
      kind: "finish",
      onBusinessApp: true,
    });
  });

  it("v4 FINISH reads waba_id", () => {
    const data = event("FINISH", { phone_number_id: "pn-1", waba_id: "w-1" });
    expect(readSessionEvent("https://www.facebook.com", data, META)).toMatchObject({
      messagingAccountId: "w-1",
    });
  });

  it.each([
    ["not JSON", "{not json"],
    ["another type", JSON.stringify({ type: "OTHER", event: "FINISH", data: {} })],
    ["a FINISH without ids", event("FINISH", {})],
    ["an unknown event", event("SOMETHING", {})],
  ])("%s → ignored", (_name, data) => {
    expect(readSessionEvent("https://www.facebook.com", data, META)).toBeNull();
  });

  it("CANCEL carries its step; ERROR its code and session id", () => {
    expect(
      readSessionEvent(
        "https://www.facebook.com",
        event("CANCEL", { current_step: "PHONE_NUMBER_SETUP" }),
        META,
      ),
    ).toEqual({ kind: "cancel", step: "PHONE_NUMBER_SETUP" });
    expect(
      readSessionEvent(
        "https://www.facebook.com",
        event("ERROR", { error_code: "3441042", session_id: "s-9", error_message: "x" }),
        META,
      ),
    ).toEqual({ kind: "error", code: 3441042, sessionId: "s-9" });
  });
});

describe("ST-04 / EM-01: steps and error sentences", () => {
  it("every step and the unknown one has words", () => {
    for (const key of [...Object.values(CANCEL_STEPS), "unknown"]) {
      expect(en.embedded.steps[key], key).toBeTruthy();
    }
    expect(cancelStepKey("PERMISSIONS")).toBe("permissions");
    expect(cancelStepKey("NOT_A_STEP")).toBe("unknown");
    expect(cancelStepKey(null)).toBe("unknown");
  });

  it("every mapped code has a sentence; the region code is its own; others are generic", () => {
    for (const key of [...Object.values(ERROR_SENTENCES), "generic"]) {
      expect(en.embedded.errors[key], key).toBeTruthy();
    }
    expect(errorSentenceKey(3441042)).toBe("region");
    expect(errorSentenceKey(200)).toBe("permission");
    expect(errorSentenceKey(123)).toBe("generic");
    expect(errorSentenceKey(null)).toBe("generic");
  });

  it("no sentence names a country (UA-05, CX-08)", () => {
    const all = JSON.stringify(en.embedded);
    expect(all).not.toMatch(/Egypt|India|\+20\b/);
  });
});

describe("ST-09: the code's 5 minutes", () => {
  it("is fresh just before and stale at 5 minutes", () => {
    expect(codeStillFresh(0, 5 * 60 * 1000 - 1)).toBe(true);
    expect(codeStillFresh(0, 5 * 60 * 1000)).toBe(false);
  });
});

describe("SD-04: FB.login's extras", () => {
  it("v5 with Coexistence offers it by default; without, sends Meta's opt-out", () => {
    expect(loginExtras("v5", true)).toEqual({ version: "v5", setup: {} });
    expect(loginExtras("v5", false)).toEqual({
      version: "v5",
      setup: {},
      features: [{ name: "whatsapp_business_app_onboarding", enabled: false }],
    });
  });

  it("v4 opts in with featureType", () => {
    expect(loginExtras("v4", true)).toMatchObject({
      featureType: "whatsapp_business_app_onboarding",
    });
    expect(loginExtras("v4", false)).toEqual({ setup: {} });
  });
});

describe("UA-01 / UA-02: the adapters", () => {
  it("Embedded Signup comes first, the assisted form stays", () => {
    expect(CONNECT_ADAPTERS.map((adapter) => adapter.key)).toEqual([
      "whatsapp-embedded",
      "whatsapp",
    ]);
    for (const adapter of CONNECT_ADAPTERS) {
      expect(adapter.roles).toEqual(["tenant_owner", "tenant_admin"]);
    }
  });

  it("Embedded Signup is offered only when the API says it is enabled", () => {
    expect(availableAdapters(false).map((adapter) => adapter.key)).toEqual(["whatsapp"]);
    expect(availableAdapters(true).map((adapter) => adapter.key)).toEqual([
      "whatsapp-embedded",
      "whatsapp",
    ]);
  });
});

describe("UD-08: the provider's disconnect reasons", () => {
  it("each known reason has words; an unknown one reads as Meta's disconnect", () => {
    const onboarding = (en as unknown as { onboarding: { reasons: Record<string, string> } })
      .onboarding;
    for (const reason of [...DISCONNECT_REASONS, "unknown"]) {
      expect(onboarding.reasons[reason], reason).toBeTruthy();
    }
    expect(disconnectReasonKey("PRIMARY_INACTIVITY")).toBe("PRIMARY_INACTIVITY");
    expect(disconnectReasonKey("SOMETHING_NEW")).toBe("unknown");
    expect(disconnectReasonKey(null)).toBe("unknown");
  });
});

describe("HM9: home's Coexistence warnings", () => {
  const base = { id: "a", status: "active", mode: null, reminderDays: null, health: "healthy" };

  it("an assisted or Cloud API number gets no reminder (M-R36)", () => {
    expect(coexistenceWarnings([base, { ...base, id: "b", mode: "cloud_api" }])).toEqual({
      reminder: null,
      disconnected: null,
    });
  });

  it("an active Coexistence number gets the reminder with the configured days", () => {
    expect(
      coexistenceWarnings([{ ...base, mode: "coexistence", reminderDays: 13 }]).reminder,
    ).toEqual({ days: 13 });
    expect(
      coexistenceWarnings([{ ...base, status: "disabled", mode: "coexistence" }]).reminder,
    ).toBeNull();
  });

  it("a number Meta disconnected is named", () => {
    expect(
      coexistenceWarnings([base, { ...base, id: "gone", health: "provider_disconnected" }])
        .disconnected,
    ).toBe("gone");
  });
});

describe("SD-06: the one route's CSP", () => {
  const nonce = "abcdefghijklmnopqrstuvwx";
  const plain = buildCsp({ nonce, apiOrigin: "https://api.iwasla.tech", devServer: false });

  it("every other route keeps the start head's policy (FGE-7, M-R34)", () => {
    expect(plain).toContain("frame-src 'none'");
    expect(plain).toContain("connect-src 'self' https://api.iwasla.tech;");
    expect(plain).not.toMatch(/facebook/);
  });

  it("the adapter's route adds Meta's SDK host and facebook.com, nothing else", () => {
    const policy = buildCsp({
      nonce,
      apiOrigin: "https://api.iwasla.tech",
      devServer: false,
      extra: embeddedSignupCsp(undefined),
    });
    expect(policy).toContain("https://connect.facebook.net");
    expect(policy).toContain("frame-src https://www.facebook.com https://*.facebook.com");
    expect(policy).not.toContain("frame-src 'none'");
    const origins = new Set(policy.match(/https:\/\/[^\s;]+/g));
    expect([...origins].sort()).toEqual(
      [
        "https://*.facebook.com",
        "https://api.iwasla.tech",
        "https://connect.facebook.net",
        "https://www.facebook.com",
      ].sort(),
    );
    expect(EMBEDDED_SIGNUP_ROUTE).toBe("/channels/connect/whatsapp-embedded");
  });

  it("the e2e build names the fakes' origin instead of Meta's", () => {
    expect(embeddedSignupCsp("http://127.0.0.1:4010")).toEqual({
      script: ["http://127.0.0.1:4010"],
      frame: ["http://127.0.0.1:4010"],
      connect: ["http://127.0.0.1:4010"],
    });
    expect(embeddedSignupCsp("none").script).toEqual(["https://connect.facebook.net"]);
  });
});

describe("SD-08: Meta's SDK lives in one file", () => {
  it("only the adapter's component names the SDK", () => {
    const shell = readFileSync(join(here, "../app/(app)/layout.tsx"), "utf8");
    expect(shell).not.toMatch(/connect\.facebook\.net|embedded-signup/);
    const component = readFileSync(
      join(here, "../components/app/channels/embedded-signup.tsx"),
      "utf8",
    );
    expect(component).toMatch(/sdkSource\(\)/);
  });

  it("the connection page and home never import the SDK's module (FGE-8)", () => {
    for (const file of [
      "../lib/coexistence.ts",
      "../components/app/channels/connection-onboarding.tsx",
      "../components/app/coexistence-warnings.tsx",
    ]) {
      const source = readFileSync(join(here, file), "utf8");
      expect(source, file).not.toMatch(/connect\.facebook\.net|lib\/embedded-signup/);
    }
  });

  it("no hook is put on a Badge, which drops data attributes (FEF-50)", () => {
    for (const file of [
      "../components/app/channels/connection-onboarding.tsx",
      "../components/inbox/thread.tsx",
    ]) {
      const source = readFileSync(join(here, file), "utf8");
      expect(source, file).not.toMatch(/<Badge[^>]*\sdata-/);
    }
  });

  it("the code is never put in storage, a URL or a log (ST-01)", () => {
    const component = readFileSync(
      join(here, "../components/app/channels/embedded-signup.tsx"),
      "utf8",
    );
    expect(component).not.toMatch(/localStorage|sessionStorage|indexedDB|console\.|searchParams/);
  });
});

describe("OS-05: the stream's two new frames", () => {
  it("are part of the vocabulary", () => {
    expect(EVENT_TYPES).toContain("message.revised");
    expect(EVENT_TYPES).toContain("whatsapp.onboarding.updated");
  });
});
