/**
 * The AI and knowledge composites (FE-6, UI6-02): a document's title, a passage and a customer's
 * question stay text (M-U35); a tool switch reports the change and draws what it is given - it never
 * moves on its own (M-U37); the scope picker names the bases chosen; the file field's one keyboard
 * stop is its button; the counter speaks only near its bound; the neutral badge keeps its edge (FEF-39).
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  AgentRow,
  AnswersMark,
  Badge,
  DocumentRow,
  DocumentStatusBadge,
  FileField,
  GapList,
  KnowledgeScopePicker,
  PassageList,
  RecordList,
  SettingSwitchRow,
  Switch,
  TextCounter,
  ToolSwitchRow,
} from "../src";

const MARKUP = '<img src=x onerror="alert(1)"><b>bold</b>';

describe("text a person, a document or the AI wrote is never markup (M-U35)", () => {
  it("a document row, a passage and a gap render as text", () => {
    const { container } = render(
      <>
        <RecordList label="Documents">
          <DocumentRow
            title={MARKUP}
            status={<DocumentStatusBadge status="ready" label="Ready" />}
            type="PDF"
            size="12 kB"
            accessibleName={`${MARKUP}, Ready`}
            onOpen={() => undefined}
          />
        </RecordList>
        <PassageList
          label="Passages"
          passages={[{ key: "1", title: MARKUP, text: MARKUP, match: "" }]}
        />
        <GapList
          label="Gaps"
          searchesLabel="Asked"
          lastAskedLabel="Last"
          gaps={[
            { key: "1", query: MARKUP, searches: "2", lastAsked: "now", lastAskedFull: "now" },
          ]}
        />
      </>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getAllByText(MARKUP).length).toBeGreaterThanOrEqual(3);
  });
  it("an agent row draws its name as text and its model left to right", () => {
    const { container } = render(
      <RecordList label="Agents">
        <AgentRow
          href="/ai/agents/1"
          name={MARKUP}
          status={<Badge>Draft</Badge>}
          answers={<AnswersMark label="Answers customers" />}
          model="gpt-4.1-mini"
          modelLabel="Model"
        />
      </RecordList>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("gpt-4.1-mini").getAttribute("dir")).toBe("ltr");
    // "Answers customers" is not a status: no status badge carries it.
    expect(screen.getByText("Answers customers").closest("[data-badge]")).toBeNull();
  });
  it("a document row is one button whose name is title, status and time", async () => {
    const onOpen = vi.fn();
    render(
      <RecordList label="Documents">
        <DocumentRow
          title="Prices"
          status={<DocumentStatusBadge status="processing" label="Indexing" />}
          type="Text"
          size="1 kB"
          accessibleName="Prices, Indexing, 2 min ago"
          onOpen={onOpen}
        />
      </RecordList>,
    );
    const row = screen.getByRole("button", { name: "Prices, Indexing, 2 min ago" });
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("a tool switch reports and waits (M-U37)", () => {
  it("Space asks for the change; the switch stays where its owner puts it", async () => {
    const onChange = vi.fn();
    render(
      <ul>
        <ToolSwitchRow
          label="Search your knowledge"
          description="Answers from documents."
          on={false}
          stateLabel="Off"
          onChange={onChange}
        />
      </ul>,
    );
    const toggle = screen.getByRole("switch", { name: "Search your knowledge" });
    toggle.focus();
    await userEvent.keyboard(" ");
    expect(onChange).toHaveBeenCalledWith(true);
    // Not optimistic: nothing re-rendered it with on=true, so it is still off.
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });
  it("busy disables it; read-only shows words and no switch; an unknown tool shows its API name", () => {
    const { rerender } = render(
      <ul>
        <ToolSwitchRow
          label="Hand over"
          description="d"
          on
          stateLabel="On"
          busy
          onChange={() => undefined}
        />
      </ul>,
    );
    expect(screen.getByRole("switch").hasAttribute("disabled")).toBe(true);
    rerender(
      <ul>
        <ToolSwitchRow
          label="lookup_order"
          apiName="lookup_order"
          description="d"
          on
          readOnly
          stateLabel="On"
        />
      </ul>,
    );
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.getByText("On")).toBeTruthy();
    expect(screen.getByText("lookup_order").getAttribute("dir")).toBe("ltr");
  });
});

describe("the knowledge scope", () => {
  it("'Only these' lists the bases and reports the ones chosen; none chosen says so", async () => {
    const onChosen = vi.fn();
    render(
      <KnowledgeScopePicker
        legend="Which knowledge it searches"
        allLabel="Every knowledge base"
        chosenLabel="Only these"
        choice="chosen"
        bases={[
          { id: "a", name: "Prices" },
          { id: "b", name: "Policies" },
        ]}
        chosen={[]}
        problem="Choose at least one knowledge base."
        onChoiceChange={() => undefined}
        onChosenChange={onChosen}
      />,
    );
    expect(screen.getByRole("alert").textContent).toBe("Choose at least one knowledge base.");
    await userEvent.click(screen.getByRole("checkbox", { name: "Prices" }));
    expect(onChosen).toHaveBeenCalledWith(["a"]);
  });
});

describe("the file field", () => {
  it("its one keyboard stop is the button; the hidden input is out of the tab order", () => {
    render(
      <FileField
        label="File"
        chooseLabel="Choose a file"
        accept=".pdf"
        problem="This file is empty."
        onFile={() => undefined}
      />,
    );
    const button = screen.getByRole("button", { name: /Choose a file/ });
    expect(button.getAttribute("aria-invalid")).toBe("true");
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    expect(input.tabIndex).toBe(-1);
    expect(input.accept).toBe(".pdf");
    expect(screen.getByRole("alert").textContent).toBe("This file is empty.");
    expect(within(screen.getByRole("group", { name: "File" })).getByRole("button")).toBe(button);
  });
  it("a chosen file is reported by name", async () => {
    const onFile = vi.fn();
    render(<FileField label="File" chooseLabel="Choose" accept=".txt" onFile={onFile} />);
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    const file = new File(["hello"], "prices.txt", { type: "text/plain" });
    await userEvent.upload(input, file);
    expect(onFile).toHaveBeenCalledWith(file);
  });
});

describe("the counter and the setting row", () => {
  it("the counter is quiet until 90 % and red past the bound", () => {
    const { rerender, container } = render(<TextCounter length={10} max={100} text="10 / 100" />);
    expect(container.firstElementChild?.getAttribute("aria-live")).toBe("off");
    rerender(<TextCounter length={95} max={100} text="95 / 100" />);
    expect(container.firstElementChild?.getAttribute("aria-live")).toBe("polite");
    rerender(<TextCounter length={101} max={100} text="101 / 100" />);
    expect(container.firstElementChild?.className).toContain("text-danger");
  });
  it("'Use the default' only for a workspace override", async () => {
    const onUseDefault = vi.fn();
    const { rerender } = render(
      <ul>
        <SettingSwitchRow
          label="Match exact words too"
          description="d"
          sourceLabel="Uses the default"
          overridden={false}
          useDefaultLabel="Use the default"
          onUseDefault={onUseDefault}
          control={({ labelId }) => <Switch aria-labelledby={labelId} />}
        />
      </ul>,
    );
    expect(screen.queryByRole("button", { name: "Use the default" })).toBeNull();
    expect(screen.getByRole("switch", { name: "Match exact words too" })).toBeTruthy();
    rerender(
      <ul>
        <SettingSwitchRow
          label="Match exact words too"
          description="d"
          sourceLabel="Set for this workspace"
          overridden
          useDefaultLabel="Use the default"
          onUseDefault={onUseDefault}
          control={({ labelId }) => <Switch aria-labelledby={labelId} />}
        />
      </ul>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Use the default" }));
    expect(onUseDefault).toHaveBeenCalledTimes(1);
  });
});

describe("FEF-39: the neutral badge keeps its edge (M-U39)", () => {
  it("the neutral pill carries the border-strong ring; status tones keep theirs", () => {
    const { container } = render(
      <>
        <Badge>Draft</Badge>
        <Badge tone="success">Ready</Badge>
      </>,
    );
    const [neutral, success] = Array.from(container.querySelectorAll("[data-badge]"));
    expect(neutral!.className).toContain("ring-border-strong");
    expect(neutral!.className).toContain("ring-inset");
    expect(success!.className).not.toContain("ring-border-strong");
  });
  it("a document status badge is a word, coloured only for ready and failed", () => {
    const { container } = render(
      <>
        <DocumentStatusBadge status="pending" label="Waiting" />
        <DocumentStatusBadge status="failed" label="Failed" />
      </>,
    );
    const tones = Array.from(container.querySelectorAll("[data-badge]")).map((badge) =>
      badge.getAttribute("data-badge"),
    );
    expect(tones).toEqual(["neutral", "danger"]);
  });
});
