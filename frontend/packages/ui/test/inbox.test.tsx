/**
 * The inbox composites (FE-4, UI4-02): customer text stays text, sides are logical, counts are read
 * as words, the segmented control and the link confirmation behave as their roles say.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageCircle } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import {
  ConversationList,
  ConversationRow,
  CountBadge,
  InertLink,
  LinkConfirm,
  MediaTile,
  MessageBubble,
  NavList,
  SegmentedControl,
  TranscriptBlock,
} from "../src";

const MARKUP = '<img src=x onerror="alert(1)"><b>bold</b>';

describe("customer text is never markup (M-V27, M-V33)", () => {
  it("a row's name and excerpt render as text", () => {
    const { container } = render(
      <ul>
        <ConversationRow
          href="/inbox/1"
          who={MARKUP}
          channelIcon={MessageCircle}
          channelIconName="MessageCircle"
          channelLabel="WhatsApp"
          previewText={MARKUP}
          time="now"
          timeFull="now"
          accessibleName="row"
        />
      </ul>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain(MARKUP);
  });
  it("a bubble's text renders as text with dir=auto", () => {
    const { container } = render(
      <ol>
        <MessageBubble side="in">{MARKUP}</MessageBubble>
      </ol>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[data-message-text]")?.getAttribute("dir")).toBe("auto");
  });
  it("a link in customer text is a button, never a navigable anchor (M-V28)", async () => {
    const activate = vi.fn();
    const { container } = render(<InertLink onActivate={activate}>https://example.test</InertLink>);
    expect(container.querySelector("a")).toBeNull();
    await userEvent.click(screen.getByRole("button"));
    expect(activate).toHaveBeenCalledTimes(1);
  });
});

describe("sides are logical (M-V30)", () => {
  it("inbound at the inline start, outbound at the inline end", () => {
    const { container } = render(
      <ol>
        <MessageBubble side="in">a</MessageBubble>
        <MessageBubble side="out">b</MessageBubble>
      </ol>,
    );
    const [inbound, outbound] = [...container.querySelectorAll("[data-bubble]")];
    expect(inbound?.className).toContain("self-start");
    expect(outbound?.className).toContain("self-end");
    for (const element of container.querySelectorAll("*")) {
      expect(element.className.toString()).not.toMatch(/\b(ml|mr|pl|pr|left|right)-/);
    }
  });
});

describe("counts and states are words", () => {
  it("a count is read as its label, not a bare number", () => {
    const { container } = render(<CountBadge value="99+" label="120 unread" />);
    expect(container.querySelector("[data-count]")?.getAttribute("aria-hidden")).toBe("true");
    expect(container.textContent).toContain("120 unread");
  });
  it("NavList draws a count beside an entry only when given", () => {
    const { container } = render(
      <NavList
        items={[
          {
            key: "inbox",
            href: "/inbox",
            label: "Inbox",
            icon: MessageCircle,
            iconName: "MessageCircle",
            count: "3",
            countLabel: "3 unread messages",
          },
          { key: "home", href: "/", label: "Home", icon: MessageCircle, iconName: "MessageCircle" },
        ]}
      />,
    );
    expect(container.querySelectorAll("[data-count]")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Inbox.*3 unread messages/ })).toBeTruthy();
  });
  it("a media tile in a state shows its words and makes no image", () => {
    for (const state of ["pending", "failed", "unavailable"] as const) {
      const { container, unmount } = render(
        <MediaTile kind="image" state={state} label={`state ${state}`} />,
      );
      expect(container.querySelector("img")).toBeNull();
      expect(container.textContent).toContain(`state ${state}`);
      unmount();
    }
  });
  it("a long transcript sits behind a disclosure", async () => {
    render(
      <TranscriptBlock
        label="Transcript"
        state="ready"
        text="line"
        showLabel="Show"
        hideLabel="Hide"
        long
      />,
    );
    const toggle = screen.getByRole("button", { name: "Show" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: "Hide" }).getAttribute("aria-expanded")).toBe("true");
  });
});

describe("controls", () => {
  it("the segmented control presses one option", async () => {
    const change = vi.fn();
    render(
      <SegmentedControl
        label="Status"
        value="open"
        onChange={change}
        options={[
          { value: "open", label: "Open" },
          { value: "closed", label: "Closed" },
        ]}
      />,
    );
    expect(screen.getByRole("button", { name: "Open" }).getAttribute("aria-pressed")).toBe("true");
    await userEvent.click(screen.getByRole("button", { name: "Closed" }));
    expect(change).toHaveBeenCalledWith("closed");
  });
  it("Load more is a button the keyboard reaches", async () => {
    const more = vi.fn();
    render(
      <ConversationList label="Conversations" loadMoreLabel="Load more" onLoadMore={more}>
        <li>a</li>
      </ConversationList>,
    );
    await userEvent.tab();
    await userEvent.keyboard("{Enter}");
    expect(more).toHaveBeenCalledTimes(1);
  });
  it("the link confirmation shows the full address before opening", async () => {
    const open = vi.fn();
    const close = vi.fn();
    render(
      <LinkConfirm
        url="https://example.test/a/very/long/path?x=1"
        title="Open this link?"
        description="Check it"
        openLabel="Open link"
        cancelLabel="Cancel"
        closeLabel="Close"
        onOpen={open}
        onClose={close}
      />,
    );
    expect(screen.getByText("https://example.test/a/very/long/path?x=1")).toBeTruthy();
    expect(open).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Open link" }));
    expect(open).toHaveBeenCalledWith("https://example.test/a/very/long/path?x=1");
  });
});
