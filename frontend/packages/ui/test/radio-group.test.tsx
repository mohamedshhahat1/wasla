/**
 * Shift+Tab leaves a RadioGroup in every browser (FE-6, KY-02 on GitHub's Firefox). The browser
 * decides where focus goes after the key's handlers have run and before React renders again, so
 * the group's container must already be out of the Tab order when the key reaches the radio.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RadioGroup } from "../src/primitives/radio-group";

const options = [
  { value: "first", label: "First" },
  { value: "second", label: "Second" },
];

describe("RadioGroup: Shift+Tab", () => {
  it("takes the container out of the Tab order before the browser moves focus", () => {
    render(<RadioGroup aria-label="Plan" defaultValue="second" options={options} />);
    const group = screen.getByRole("radiogroup");
    const radio = screen.getByRole("radio", { name: "Second" });
    radio.focus();
    let seen: number | null = null;
    // A listener on the radio itself runs after the group's capture handler and before any render.
    radio.addEventListener("keydown", () => {
      seen = group.tabIndex;
    });
    fireEvent.keyDown(radio, { key: "Tab", shiftKey: true });
    expect(seen).toBe(-1);
  });

  it("leaves forward Tab and the arrow keys to Radix", () => {
    render(<RadioGroup aria-label="Plan" defaultValue="second" options={options} />);
    const group = screen.getByRole("radiogroup");
    const radio = screen.getByRole("radio", { name: "Second" });
    radio.focus();
    let seen: number | null = null;
    radio.addEventListener("keydown", () => {
      seen = group.tabIndex;
    });
    fireEvent.keyDown(radio, { key: "Tab" });
    expect(seen).toBe(0);
    fireEvent.keyDown(radio, { key: "ArrowDown" });
    expect(seen).toBe(0);
  });

  it("still calls a caller's own capture handler", () => {
    const keys: string[] = [];
    render(
      <RadioGroup
        aria-label="Plan"
        defaultValue="second"
        options={options}
        onKeyDownCapture={(event) => keys.push(event.key)}
      />,
    );
    fireEvent.keyDown(screen.getByRole("radio", { name: "Second" }), {
      key: "Tab",
      shiftKey: true,
    });
    expect(keys).toEqual(["Tab"]);
  });
});
