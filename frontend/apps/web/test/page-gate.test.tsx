// @vitest-environment jsdom
/** FEF-17: "connection lost" keeps an auth page's form; every other page shows the retry. */
import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";

import { PageGate } from "../components/page-gate";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/login",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({
    status: "connection-lost",
    profile: null,
    profileLoaded: true,
    session: { restore: vi.fn(() => Promise.resolve()) },
  }),
}));

afterEach(cleanup);

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
      {children}
    </NextIntlClientProvider>
  );
}

const lost = (messagesFor("en")["auth"] as Record<string, string>)["connectionLostTitle"]!;

describe("PageGate when the session routes cannot answer (FEF-17)", () => {
  it("an auth page keeps its form", () => {
    render(
      <PageGate need="signed-out">
        <form aria-label="log in" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByRole("form", { name: "log in" })).toBeTruthy();
    expect(screen.queryByText(lost)).toBeNull();
  });

  it("a signed-in page shows the connection-lost state with its retry", () => {
    render(
      <PageGate need="workspace">
        <form aria-label="home" />
      </PageGate>,
      { wrapper: Wrapper },
    );
    expect(screen.getByText(lost)).toBeTruthy();
    expect(screen.queryByRole("form", { name: "home" })).toBeNull();
  });
});
