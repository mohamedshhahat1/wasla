// @vitest-environment jsdom
/**
 * AU-01, AU-02, AU-04: tokens leave the address bar before anything runs; every error code a
 * screen can receive is shown as its catalogue sentence with the request id - never the
 * backend's sentence - in English and Arabic.
 */
import { cleanup, render, renderHook, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiError } from "@wasla/api-client";
import { messagesFor } from "@wasla/i18n";

import { FormAlert } from "../components/auth/form-alert";
import {
  useParamsFromAddress,
  useTokenFromAddress,
} from "../components/auth/use-token-from-address";

import enErrors from "../../../packages/i18n/messages/en/errors.json" with { type: "json" };

afterEach(cleanup);

function wrap(locale: "en" | "ar") {
  const messages = messagesFor(locale);
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <NextIntlClientProvider locale={locale} messages={messages} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    );
  };
}

describe("tokens are removed from the address bar first (AU-02)", () => {
  it("reset-password: the token is read, then the query is gone from the URL and the entry", () => {
    window.history.pushState({ keep: false }, "", "/reset-password?token=secret-reset-token#x");
    const { result } = renderHook(() => useTokenFromAddress("token"));
    expect(result.current).toBe("secret-reset-token");
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("secret-reset-token");
    expect(JSON.stringify(window.history.state)).not.toContain("secret-reset-token");
  });

  it("Google's return: code and state removed together", () => {
    window.history.pushState(null, "", "/auth/google/callback?code=c-123&state=s-456");
    const { result } = renderHook(() => useParamsFromAddress(["code", "state", "error"] as const));
    expect(result.current).toEqual({ code: "c-123", state: "s-456", error: null });
    expect(window.location.href).not.toContain("c-123");
    expect(window.location.href).not.toContain("s-456");
  });

  it("a page without a token reads null and leaves the path alone", () => {
    window.history.pushState(null, "", "/invitations/accept");
    const { result } = renderHook(() => useTokenFromAddress("token"));
    expect(result.current).toBeNull();
    expect(window.location.pathname).toBe("/invitations/accept");
  });
});

const codes = Object.keys(enErrors).filter((key) => key !== "_meta" && key !== "fallback");

describe("every error code is shown by code, with the request id (AU-01, AU-04)", () => {
  for (const locale of ["en", "ar"] as const) {
    const errors = messagesFor(locale)["errors"] as Record<string, string>;
    it(`${locale}: ${codes.length} codes → the catalogue sentence, never the backend's`, () => {
      for (const code of codes) {
        const error = new ApiError({
          status: 400,
          code,
          message: "BACKEND SENTENCE that must not show",
          requestId: `req-${code}`,
        });
        const { unmount } = render(<FormAlert error={error} />, { wrapper: wrap(locale) });
        const alert = screen.getByRole("alert");
        expect(alert.getAttribute("data-error-code")).toBe(code);
        expect(alert.textContent).not.toContain("BACKEND SENTENCE");
        if (code !== "rate_limited") expect(alert.textContent).toContain(errors[code]);
        expect(screen.getByTestId("request-id").textContent).toContain(`req-${code}`);
        unmount();
      }
    });

    it(`${locale}: rate_limited shows the seconds to wait`, () => {
      const error = new ApiError({ status: 429, code: "rate_limited", retryAfterSeconds: 42 });
      render(<FormAlert error={error} />, { wrapper: wrap(locale) });
      const text = screen.getByRole("alert").textContent ?? "";
      expect(text).toContain(errors["rate_limited"]);
      expect(text).toMatch(/42|٤٢/);
    });
  }

  it("a wrong password and an unknown account read the same (no enumeration)", () => {
    const one = render(
      <FormAlert error={new ApiError({ status: 401, code: "invalid_credentials" })} />,
      {
        wrapper: wrap("en"),
      },
    );
    const wrongPassword = screen.getByRole("alert").textContent;
    one.unmount();
    render(<FormAlert error={new ApiError({ status: 401, code: "unauthenticated" })} />, {
      wrapper: wrap("en"),
    });
    expect(wrongPassword).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBeTruthy();
  });

  it("an error that is not an ApiError is unexpected_response, not a stack", () => {
    render(<FormAlert error={new Error("TypeError: x is undefined at y.ts:3")} />, {
      wrapper: wrap("en"),
    });
    const alert = screen.getByRole("alert");
    expect(alert.getAttribute("data-error-code")).toBe("unexpected_response");
    expect(alert.textContent).not.toContain("TypeError");
  });
});
