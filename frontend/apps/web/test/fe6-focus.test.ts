// @vitest-environment jsdom
/**
 * KY-02 on GitHub (FE-6, push 1): Firefox and WebKit lost the keyboard where a pressed control was
 * removed. focusIfLost gives a lost focus to a known element and never moves a focus a person placed.
 */
import { afterEach, describe, expect, it } from "vitest";

import { focusIfLost, isFocusLost } from "../lib/focus";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("focusIfLost", () => {
  it("focuses the target when the focused control was removed", () => {
    document.body.innerHTML =
      '<button id="pressed">Activate</button><h1 id="t" tabindex="-1">A</h1>';
    const pressed = document.getElementById("pressed")!;
    pressed.focus();
    pressed.remove();
    expect(isFocusLost()).toBe(true);
    focusIfLost(document.getElementById("t"));
    expect(document.activeElement?.id).toBe("t");
  });

  it("leaves a focus the person placed where it is", () => {
    document.body.innerHTML = '<input id="field" /><h1 id="t" tabindex="-1">A</h1>';
    document.getElementById("field")!.focus();
    expect(isFocusLost()).toBe(false);
    focusIfLost(document.getElementById("t"));
    expect(document.activeElement?.id).toBe("field");
  });

  it("does nothing without a target", () => {
    expect(() => focusIfLost(null)).not.toThrow();
  });
});
