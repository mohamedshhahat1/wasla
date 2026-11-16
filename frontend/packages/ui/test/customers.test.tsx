/**
 * The customers composites (FE-5, UI5-02): customer and member text stays text, a link in a note
 * asks first, the status menu offers only the moves it is given, the inline edit keeps to the
 * keyboard's rules, and a value outside a bound is never sent.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  Badge,
  ConsentHistory,
  ContactRow,
  FieldConfirmedMark,
  IdentityList,
  InlineEditField,
  LeadRow,
  NoteComposer,
  NoteList,
  PreviewNotice,
  RecordHeader,
  RecordList,
  RecordPanel,
  ScoreField,
  StatusMenu,
  Timeline,
} from "../src";

const MARKUP = '<img src=x onerror="alert(1)"><b>bold</b>';
const counter = (length: number, max: number) => `${length} / ${max}`;

describe("customer text is never markup (M-T34, M-T35)", () => {
  it("a contact row's name, number and lead render as text, the number left to right", () => {
    const { container } = render(
      <RecordList label="Contacts">
        <ContactRow
          href="/contacts/1"
          who={MARKUP}
          handle="+201055501234"
          leadName={MARKUP}
          consent={<Badge tone="warning">Opted out</Badge>}
          time="2 h"
          timeFull="Today 10:00"
          stateLabel="Opted out"
        />
      </RecordList>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain(MARKUP);
    expect(container.querySelector('bdi[dir="ltr"]')?.textContent).toBe("+201055501234");
    expect(screen.getByRole("list", { name: "Contacts" })).toBeTruthy();
    expect(screen.getByRole("link").getAttribute("href")).toBe("/contacts/1");
  });

  it("a lead row's name, interest and facts render as text", () => {
    const { container } = render(
      <ul>
        <LeadRow
          href="/leads/1"
          who={MARKUP}
          status={<Badge>New</Badge>}
          interest={MARKUP}
          facts={[{ key: "owner", label: "Owner", value: <bdi>{MARKUP}</bdi> }]}
        />
      </ul>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector('bdi[dir="auto"]')?.textContent).toBe(MARKUP);
  });

  it("a record's heading is the name inside a bdi", () => {
    const { container } = render(<RecordHeader title={MARKUP} />);
    expect(container.querySelector("h1 bdi")?.textContent).toBe(MARKUP);
    expect(container.querySelector("img")).toBeNull();
  });

  it("a record panel is a section on the secondary surface that carries its marker", () => {
    const { container } = render(
      <RecordPanel data-section="score">
        <p>Score</p>
      </RecordPanel>,
    );
    const panel = container.querySelector("section[data-record-panel]");
    expect(panel?.getAttribute("data-section")).toBe("score");
    expect(panel?.className).toContain("bg-secondary-subtle");
  });

  it("a note renders as text with its line breaks kept and dir=auto", () => {
    const { container } = render(
      <NoteList
        label="Notes"
        empty="No notes"
        onLink={() => undefined}
        notes={[
          {
            key: "1",
            author: MARKUP,
            time: "now",
            timeFull: "now",
            parts: [{ kind: "text", text: `${MARKUP}\nsecond line` }],
          },
        ]}
      />,
    );
    const text = container.querySelector("[data-note-text]");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(text?.getAttribute("dir")).toBe("auto");
    expect(text?.className).toContain("whitespace-pre-wrap");
    expect(text?.textContent).toBe(`${MARKUP}\nsecond line`);
  });

  it("a link in a note is a button that asks first, never a navigable anchor (M-T44)", async () => {
    const onLink = vi.fn();
    const { container } = render(
      <NoteList
        label="Notes"
        empty="No notes"
        onLink={onLink}
        notes={[
          {
            key: "1",
            author: "Mona",
            time: "now",
            timeFull: "now",
            parts: [
              { kind: "text", text: "see " },
              { kind: "link", text: "https://example.test/a", url: "https://example.test/a" },
            ],
          },
        ]}
      />,
    );
    expect(container.querySelector("a")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "https://example.test/a" }));
    expect(onLink).toHaveBeenCalledWith("https://example.test/a");
  });

  it("notes have no edit or delete control", () => {
    render(
      <NoteList
        label="Notes"
        empty="No notes"
        onLink={() => undefined}
        notes={[
          {
            key: "1",
            author: "Mona",
            time: "now",
            timeFull: "now",
            parts: [{ kind: "text", text: "hi" }],
          },
        ]}
      />,
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("StatusMenu (M-T27)", () => {
  it("offers exactly the moves it is given, in their order", async () => {
    const onMove = vi.fn();
    render(
      <StatusMenu
        current={<Badge>New</Badge>}
        changeLabel="Change status"
        moves={[
          { value: "contacted", label: "Contacted" },
          { value: "lost", label: "Lost" },
        ]}
        onMove={onMove}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Change status" }));
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(["Contacted", "Lost"]);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(onMove).toHaveBeenCalledTimes(1);
  });

  it("draws no menu when there is no move, only the words for it", () => {
    render(
      <StatusMenu
        current={<Badge tone="success">Won</Badge>}
        changeLabel="Change status"
        closedLabel="Won - closed"
        moves={[]}
        onMove={() => undefined}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Won - closed")).toBeTruthy();
  });

  it("is busy, and cannot be opened, while a move is in flight", () => {
    render(
      <StatusMenu
        current={<Badge>New</Badge>}
        changeLabel="Change status"
        moves={[{ value: "lost", label: "Lost" }]}
        busy
        onMove={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Change status" })).toHaveProperty("disabled", true);
  });
});

function Editor({
  onSave,
  error,
}: {
  onSave: (value: string) => Promise<unknown>;
  error?: string;
}) {
  return (
    <InlineEditField
      label="Name"
      value="Nour"
      editLabel="Edit name"
      saveLabel="Save"
      cancelLabel="Cancel"
      maxLength={10}
      counter={counter}
      error={error}
      onSave={onSave}
    />
  );
}

describe("InlineEditField", () => {
  it("Enter saves the trimmed value and focus returns to the control that opened it", async () => {
    const onSave = vi.fn(() => Promise.resolve());
    render(<Editor onSave={onSave} />);
    await userEvent.click(screen.getByRole("button", { name: "Edit name" }));
    const field = screen.getByRole("textbox", { name: "Name" });
    expect(document.activeElement).toBe(field);
    await userEvent.clear(field);
    await userEvent.type(field, "  Mona {Enter}");
    expect(onSave).toHaveBeenCalledWith("Mona");
    const opener = await screen.findByRole("button", { name: "Edit name" });
    expect(document.activeElement).toBe(opener);
  });

  it("Escape cancels without saving and focus returns", async () => {
    const onSave = vi.fn(() => Promise.resolve());
    render(<Editor onSave={onSave} />);
    await userEvent.click(screen.getByRole("button", { name: "Edit name" }));
    await userEvent.type(screen.getByRole("textbox"), "x{Escape}");
    expect(onSave).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Edit name" }));
  });

  it("an unchanged value is not sent", async () => {
    const onSave = vi.fn(() => Promise.resolve());
    render(<Editor onSave={onSave} />);
    await userEvent.click(screen.getByRole("button", { name: "Edit name" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Edit name" })).toBeTruthy();
  });

  it("a refused save keeps the field open with its error, and the bound holds", async () => {
    const onSave = vi.fn(() => Promise.reject(new Error("no")));
    render(<Editor onSave={onSave} error="Enter a name, or remove it." />);
    await userEvent.click(screen.getByRole("button", { name: "Edit name" }));
    const field = screen.getByRole<HTMLInputElement>("textbox");
    await userEvent.clear(field);
    await userEvent.type(field, "abcdefghijklmnop");
    expect(field.value).toBe("abcdefghij");
    expect(screen.getByText("10 / 10")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Enter a name, or remove it.",
    );
    expect(screen.getByRole("textbox")).toBeTruthy();
  });
});

describe("ScoreField", () => {
  it("sends a changed value inside the bounds and nothing else", async () => {
    const onSave = vi.fn();
    render(
      <ScoreField
        label="Score"
        hint="0 to 100"
        value={40}
        min={0}
        max={100}
        saveLabel="Save score"
        onSave={onSave}
      />,
    );
    const field = screen.getByRole<HTMLInputElement>("textbox", { name: "Score" });
    const save = screen.getByRole("button", { name: "Save score" });
    expect(save).toHaveProperty("disabled", true);
    await userEvent.clear(field);
    await userEvent.type(field, "250");
    expect(save).toHaveProperty("disabled", true);
    await userEvent.type(field, "{Enter}");
    expect(onSave).not.toHaveBeenCalled();
    await userEvent.clear(field);
    await userEvent.type(field, "-7a5");
    expect(field.value).toBe("75");
    await userEvent.click(save);
    expect(onSave).toHaveBeenCalledWith(75);
  });
});

function Composer({ onSubmit, max = 20 }: { onSubmit: () => void; max?: number }) {
  const [value, setValue] = useState("");
  return (
    <NoteComposer
      label="Add a note"
      hint="Notes are never sent to the customer."
      value={value}
      maxLength={max}
      counter={counter}
      submitLabel="Add note"
      onChange={setValue}
      onSubmit={onSubmit}
    />
  );
}

describe("NoteComposer", () => {
  it("an empty or blank note is never sent", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const add = screen.getByRole("button", { name: "Add note" });
    expect(add).toHaveProperty("disabled", true);
    await userEvent.type(screen.getByRole("textbox"), "   {Control>}{Enter}{/Control}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("Ctrl + Enter and the button add it; Enter alone is a new line", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const field = screen.getByRole<HTMLTextAreaElement>("textbox");
    await userEvent.type(field, "first{Enter}second");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(field.value).toBe("first\nsecond");
    await userEvent.type(field, "{Control>}{Enter}{/Control}");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });

  it("keeps to its bound and says how much is used", async () => {
    render(<Composer onSubmit={() => undefined} max={5} />);
    const field = screen.getByRole<HTMLTextAreaElement>("textbox");
    await userEvent.type(field, "abcdefgh");
    expect(field.value).toBe("abcde");
    expect(screen.getByText("5 / 5")).toBeTruthy();
  });
});

describe("lists that say what they are", () => {
  it("identities are copied, never edited, and read left to right", async () => {
    const onCopy = vi.fn();
    const { container } = render(
      <IdentityList
        label="Identities"
        onCopy={onCopy}
        items={[
          {
            key: "1",
            channel: "WhatsApp",
            kind: "Phone number",
            value: "201055501234",
            copyLabel: "Copy number",
          },
        ]}
      />,
    );
    expect(container.querySelector("input")).toBeNull();
    expect(container.querySelector('bdi[dir="ltr"]')?.textContent).toBe("201055501234");
    await userEvent.click(screen.getByRole("button", { name: "Copy number" }));
    expect(onCopy).toHaveBeenCalledWith("201055501234");
  });

  it("the consent history and the timeline are ordered lists with a name, or say they are empty", () => {
    const { rerender } = render(
      <ConsentHistory label="Consent history" empty="Nothing recorded yet." entries={[]} />,
    );
    expect(screen.getByText("Nothing recorded yet.")).toBeTruthy();
    rerender(
      <ConsentHistory
        label="Consent history"
        empty="Nothing recorded yet."
        entries={[
          {
            key: "1",
            what: "Opted out by the customer",
            channel: "WhatsApp",
            time: "now",
            timeFull: "now",
          },
        ]}
      />,
    );
    expect(
      within(screen.getByRole("list", { name: "Consent history" })).getAllByRole("listitem"),
    ).toHaveLength(1);
    rerender(
      <Timeline
        label="Activity"
        empty="Nothing yet."
        entries={[
          { key: "1", what: <span>Mona changed the status</span>, time: "now", timeFull: "now" },
        ]}
      />,
    );
    expect(screen.getByRole("list", { name: "Activity" })).toBeTruthy();
  });

  it("the confirmed mark and the preview notice are words, not colour alone", () => {
    render(
      <>
        <FieldConfirmedMark label="Confirmed by a person" />
        <PreviewNotice title="This is a preview" body="There is no server behind it." />
      </>,
    );
    expect(screen.getByText("Confirmed by a person")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "This is a preview" })).toBeTruthy();
  });
});
