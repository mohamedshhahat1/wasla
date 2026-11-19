/**
 * The team, settings and account composites (FE-7, UI7-02): names, addresses and an unknown
 * action stay text, never markup; a row draws exactly the actions the screen gives it and none of
 * its own (the role rule lives in the screen: absent, not disabled); the settings navigation marks
 * the current page; the danger zone is a labelled section.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActivityRow, DangerZone, InvitationRow, MemberRow, RoleBadge, SettingsNav } from "../src";

const MARKUP = '<img src=x onerror="alert(1)"><b>bold</b>';

describe("what people typed is text (UI7-04)", () => {
  it("a member's name and address, an invitation's address and an activity row", () => {
    const { container } = render(
      <ul>
        <MemberRow
          name={MARKUP}
          email={MARKUP}
          role={<RoleBadge tone="member">Member</RoleBadge>}
          when={null}
          whenFull={null}
        />
        <InvitationRow
          email={MARKUP}
          role={<RoleBadge tone="member">Member</RoleBadge>}
          state="Expired"
          expired
          stateFull={null}
        />
        <ActivityRow
          actor={MARKUP}
          action={MARKUP}
          actionIsCode
          target={MARKUP}
          when="now"
          whenFull="now"
        />
      </ul>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getAllByText(MARKUP).length).toBeGreaterThanOrEqual(5);
  });

  it("names and addresses are isolated for a page read from the other side", () => {
    const { container } = render(
      <ul>
        <MemberRow
          name="منى"
          email="mona@example.test"
          role={<RoleBadge tone="member">Member</RoleBadge>}
          when={null}
          whenFull={null}
        />
      </ul>,
    );
    const isolated = Array.from(container.querySelectorAll("bdi")).map((node) => node.textContent);
    expect(isolated).toEqual(["منى", "mona@example.test"]);
    expect(container.querySelectorAll("bdi")[1]?.getAttribute("dir")).toBe("ltr");
  });
});

describe("a row draws only what the screen gives it (TM-03)", () => {
  it("no actions given → no button at all, not a disabled one", () => {
    render(
      <ul>
        <MemberRow
          name="Sara"
          email="sara@example.test"
          role={<RoleBadge tone="member">Member</RoleBadge>}
          when="Joined"
          whenFull="x"
        />
      </ul>,
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("the last owner's note is said instead of an action", () => {
    render(
      <ul>
        <MemberRow
          name="Mona"
          email="mona@example.test"
          role={<RoleBadge tone="owner">Owner</RoleBadge>}
          youLabel="You"
          when={null}
          whenFull={null}
          note="Make someone else the owner first."
        />
      </ul>,
    );
    expect(screen.getByText("Make someone else the owner first.")).toBeTruthy();
    expect(screen.getByText("(You)")).toBeTruthy();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("a removed person's row is marked", () => {
    const { container } = render(
      <ul>
        <MemberRow
          name="Sara"
          email="s@example.test"
          role={<RoleBadge tone="member">Member</RoleBadge>}
          removed
          when={null}
          whenFull={null}
        />
      </ul>,
    );
    expect(container.querySelector("[data-removed]")).not.toBeNull();
  });
});

describe("invitations and roles", () => {
  it("an expired invitation is said in words and marked", () => {
    const { container } = render(
      <ul>
        <InvitationRow
          email="late@example.test"
          role={<RoleBadge tone="admin">Administrator</RoleBadge>}
          state="Expired"
          expired
          stateFull="17 November 2026"
          language="Türkçe"
          languageLabel="E-mail in"
        />
      </ul>,
    );
    expect(container.querySelector("[data-expired]")).not.toBeNull();
    expect(screen.getByText("Expired")).toBeTruthy();
    expect(screen.getByText("Türkçe")).toBeTruthy();
    expect(container.querySelector('[data-role-badge="admin"]')?.textContent).toBe("Administrator");
  });
});

describe("the settings navigation", () => {
  it("every entry is a link and the current page is marked", () => {
    render(
      <SettingsNav
        label="Settings pages"
        current="activity"
        items={[
          { key: "workspace", href: "/settings/workspace", label: "Workspace" },
          { key: "activity", href: "/settings/activity", label: "Activity" },
        ]}
        renderLink={({ children, ...props }) => <a {...props}>{children}</a>}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Settings pages" });
    const links = within(nav).getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[1]?.getAttribute("aria-current")).toBe("page");
    expect(links[0]?.getAttribute("aria-current")).toBeNull();
  });
});

describe("the danger zone", () => {
  it("is a section named by its heading, with its one action", () => {
    render(
      <DangerZone
        title="Close workspace"
        action={<button type="button">Close this workspace</button>}
      >
        <p>Everybody loses access at once.</p>
      </DangerZone>,
    );
    const region = screen.getByRole("region", { name: "Close workspace" });
    expect(within(region).getByRole("button", { name: "Close this workspace" })).toBeTruthy();
  });
});
