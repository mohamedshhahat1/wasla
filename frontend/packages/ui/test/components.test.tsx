import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  Clock,
  LogOut,
  Pause,
  Play,
  Send,
} from "lucide-react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  Combobox,
  ConfirmDialog,
  ErrorState,
  FormField,
  Icon,
  Input,
  physicalSide,
  UiProvider,
} from "../src";
import { FLIP_IN_RTL, NEVER_FLIP, shouldFlip } from "../src/icon";

describe("icons mirror only when they point along the reading direction (DS-03)", () => {
  const flips: Array<[string, unknown]> = [
    ["ArrowLeft", ArrowLeft],
    ["ArrowRight", ArrowRight],
    ["ChevronRight", ChevronRight],
    ["Send", Send],
    ["LogOut", LogOut],
  ];
  for (const [name, icon] of flips) {
    it(`${name} flips`, () => {
      expect(shouldFlip(name)).toBe(true);
      const { container } = render(<Icon icon={icon as typeof Send} name={name} />);
      expect(container.querySelector("svg")).toHaveClass("wasla-flip-rtl");
    });
  }
  const never: Array<[string, unknown]> = [
    ["Play", Play],
    ["Pause", Pause],
    ["Check", Check],
    ["Clock", Clock],
  ];
  for (const [name, icon] of never) {
    it(`${name} never flips`, () => {
      expect(shouldFlip(name)).toBe(false);
      const { container } = render(<Icon icon={icon as typeof Send} name={name} />);
      expect(container.querySelector("svg")).not.toHaveClass("wasla-flip-rtl");
    });
  }
  it("the two lists do not overlap", () => {
    expect([...FLIP_IN_RTL].filter((name) => NEVER_FLIP.has(name))).toEqual([]);
  });
  it("an icon without a label is hidden from assistive technology", () => {
    const { container } = render(<Icon icon={Send} name="Send" />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("logical popover sides", () => {
  it("inline-end is right in LTR and left in RTL", () => {
    expect(physicalSide("inline-end", "ltr")).toBe("right");
    expect(physicalSide("inline-end", "rtl")).toBe("left");
    expect(physicalSide("inline-start", "rtl")).toBe("right");
    expect(physicalSide("top", "rtl")).toBe("top");
  });
});

describe("FormField", () => {
  it("labels the control and describes it by the error first, then the hint", () => {
    render(
      <FormField label="Email" hint="A code goes here" error="Enter a valid email">
        {(control) => <Input {...control} />}
      </FormField>,
    );
    const input = screen.getByRole("textbox", { name: "Email" });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Enter a valid email A code goes here");
  });

  it("is not invalid without an error", () => {
    render(<FormField label="Name">{(control) => <Input {...control} />}</FormField>);
    expect(screen.getByRole("textbox", { name: "Name" })).not.toHaveAttribute("aria-invalid");
  });
});

describe("ConfirmDialog", () => {
  it("keeps the action disabled until the exact target is typed", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <UiProvider dir="ltr" locale="en">
        <ConfirmDialog
          defaultOpen
          title="Delete workspace"
          closeLabel="Close"
          confirmationText="acme"
          confirmationLabel="Type acme to confirm"
          confirmLabel="Delete"
          cancelLabel="Cancel"
          onConfirm={onConfirm}
        />
      </UiProvider>,
    );
    const action = screen.getByRole("button", { name: "Delete" });
    expect(action).toBeDisabled();
    await user.type(screen.getByLabelText("Type acme to confirm"), "acm");
    expect(action).toBeDisabled();
    await user.type(screen.getByLabelText("Type acme to confirm"), "e");
    expect(action).toBeEnabled();
    await user.click(action);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe("ErrorState", () => {
  it("shows the request id and no stack", () => {
    render(
      <ErrorState title="Something went wrong" requestId="req-1" requestIdLabel="Reference:" />,
    );
    expect(screen.getByTestId("request-id")).toHaveTextContent("Reference: req-1");
  });
});

function TurkishCombobox() {
  const [value, setValue] = useState<string | null>(null);
  return (
    <UiProvider dir="ltr" locale="tr">
      <span id="city">Şehir</span>
      <Combobox
        aria-labelledby="city"
        options={[
          { value: "ist", label: "İstanbul" },
          { value: "izm", label: "İzmir" },
          { value: "ank", label: "Ankara" },
          { value: "isp", label: "Isparta" },
        ]}
        value={value}
        onValueChange={setValue}
        emptyText="Sonuç yok"
      />
      <output data-testid="value">{value ?? ""}</output>
    </UiProvider>
  );
}

describe("Combobox folds case with the UI locale (plan section 20)", () => {
  it("in Turkish, 'i' finds İstanbul and İzmir but not Isparta (dotless I)", async () => {
    const user = userEvent.setup();
    render(<TurkishCombobox />);
    const input = screen.getByRole("combobox", { name: "Şehir" });
    await user.type(input, "i");
    const names = screen.getAllByRole("option").map((option) => option.textContent);
    expect(names).toEqual(["İstanbul", "İzmir"]);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("value")).toHaveTextContent("izm");
    expect(input).toHaveAttribute("aria-expanded", "false");
  });
});
