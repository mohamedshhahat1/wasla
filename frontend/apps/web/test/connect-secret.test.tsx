// @vitest-environment jsdom
/**
 * CN-04 (M-Z17): the access token typed into the connect form is posted once and kept nowhere -
 * not in TanStack's mutation or query caches, not in the URL or history, not in storage, not in
 * the form after a success. After a refusal it stays in the form's memory only.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messagesFor } from "@wasla/i18n";

const TOKEN = "EAAG-fake-token-for-the-cache-test-1234567890";
const push = vi.fn();
const post = vi.fn<(path: string, init: { body: Record<string, unknown> }) => Promise<unknown>>();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  usePathname: () => "/channels/connect/whatsapp",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("../components/session-provider", () => ({
  useSession: () => ({ session: { api: { POST: post, GET: vi.fn() } } }),
}));

vi.mock("../components/app/workspace-context", () => ({
  useWorkspace: () => ({
    role: "tenant_owner",
    scope: { workspaceId: "ws-1", sessionGeneration: 1 },
    recheck: () => Promise.resolve(),
  }),
}));

vi.mock("../components/app/channels/queries", () => ({
  useChannelReads: () => ({
    capacity: {
      data: { remaining: 1, breakdown: { allowed_channel_types: ["whatsapp"] } },
    },
  }),
  useChannelsChanged: () => () => Promise.resolve(),
}));

const { ConnectAdapterPage } = await import("../components/app/channels/connect-adapter");

let client: QueryClient;

function renderForm() {
  client = new QueryClient();
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en" messages={messagesFor("en")} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
  return render(<ConnectAdapterPage adapterKey="whatsapp" />, { wrapper: Wrapper });
}

function fill() {
  fireEvent.change(screen.getByLabelText(/Phone number ID/), { target: { value: "PNID-E2E-1" } });
  fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: TOKEN } });
}

function nowhere() {
  expect(
    JSON.stringify(
      client
        .getMutationCache()
        .getAll()
        .map((m) => m.state.variables),
    ),
  ).not.toContain(TOKEN);
  expect(client.getMutationCache().getAll()).toHaveLength(0);
  expect(
    JSON.stringify(
      client
        .getQueryCache()
        .getAll()
        .map((q) => [q.queryKey, q.state.data]),
    ),
  ).not.toContain(TOKEN);
  expect(window.location.href).not.toContain(TOKEN);
  expect(JSON.stringify(window.history.state ?? null)).not.toContain(TOKEN);
  // Storage, cookies, IndexedDB and the DOM are searched in the browser by JR-07.
  expect(push.mock.calls.flat().join(" ")).not.toContain(TOKEN);
}

beforeEach(() => {
  post.mockReset();
  push.mockReset();
});
afterEach(cleanup);

describe("the access token is a secret end to end (CN-04)", () => {
  it("the field is a password field that never autocompletes or spell-checks", () => {
    renderForm();
    const field = screen.getByLabelText<HTMLInputElement>(/Access token/);
    expect(field.type).toBe("password");
    expect(field.autocomplete).toBe("off");
    expect(field.getAttribute("spellcheck")).toBe("false");
    expect(field.getAttribute("autocapitalize")).toBe("off");
    expect(field.value).toBe("");
  });

  it("after a success: posted once, the form reset, the detail opened, the token nowhere", async () => {
    post.mockResolvedValue({
      data: { id: "acc-1" },
      response: new Response(null, { status: 201 }),
    });
    renderForm();
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Connect the number" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/channels/acc-1"));
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0]![1].body.access_token).toBe(TOKEN);
    expect(screen.getByLabelText<HTMLInputElement>(/Access token/).value).toBe("");
    expect(document.body.innerHTML).not.toContain(TOKEN);
    nowhere();
  });

  it("after a refusal: its own sentence, the token kept in the form only", async () => {
    post.mockResolvedValue({
      error: {
        error: {
          code: "whatsapp_number_already_connected",
          message: "That WhatsApp number is already connected.",
        },
      },
      response: new Response(null, { status: 409 }),
    });
    renderForm();
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Connect the number" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("already connected to Wasla");
    expect(alert.textContent).not.toContain("That WhatsApp number is already connected.");
    expect(screen.getByLabelText<HTMLInputElement>(/Access token/).value).toBe(TOKEN);
    nowhere();
  });
});
