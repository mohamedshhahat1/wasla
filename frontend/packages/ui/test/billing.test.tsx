/**
 * FE-8's billing composites (UI8-02): untrusted text stays text, the meter says what it measures
 * and draws no bar when unlimited, the series' table holds the same numbers as its chart, and the
 * payment panel's outcome is words, never colour alone.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  BarSeries,
  InvitationRow,
  InvoiceLines,
  PaymentStatusPanel,
  RoleBadge,
  UsageMeter,
} from "../src";

const MARKUP = '<img src=x onerror="alert(1)"><b>bold</b>';

describe("the billing composites", () => {
  it("keeps an invoice line's description as text", () => {
    const { container } = render(
      <InvoiceLines
        label="Lines"
        descriptionLabel="Description"
        quantityLabel="Quantity"
        amountLabel="Amount"
        lines={[{ key: "1", description: MARKUP, quantity: "1", amount: "EGP 99.00" }]}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(MARKUP)).toBeTruthy();
    expect(screen.getByRole("table", { name: "Lines" })).toBeTruthy();
  });

  it("measures against a limit, and draws no bar when unlimited", () => {
    const { rerender } = render(
      <UsageMeter label="AI replies" valueText="100 of 100" used={100} limit={100} over />,
    );
    const meter = screen.getByRole("meter", { name: "AI replies" });
    expect(meter.getAttribute("aria-valuenow")).toBe("100");
    expect(meter.getAttribute("aria-valuemax")).toBe("100");
    expect(meter.getAttribute("aria-valuetext")).toBe("100 of 100");
    rerender(<UsageMeter label="Storage" valueText="12 used" used={12} limit={null} />);
    expect(screen.queryByRole("meter")).toBeNull();
    expect(screen.getByText("12 used")).toBeTruthy();
  });

  it("shows the series' numbers as a table, the chart hidden from assistive technology", () => {
    const points = [
      { label: "19 Nov", value: 3, valueText: "3" },
      { label: "20 Nov", value: 7, valueText: "7" },
    ];
    const { container } = render(
      <BarSeries
        title="Messages per day"
        points={points}
        dayLabel="Day"
        valueLabel="Messages"
        showTable="Show as table"
        showChart="Show as chart"
        emptyText="Nothing yet."
      />,
    );
    expect(container.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(container.querySelectorAll("rect")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Show as table" }));
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("7")).toBeTruthy();
  });

  it("says when there is nothing to chart", () => {
    render(
      <BarSeries
        title="Messages per day"
        points={[]}
        dayLabel="Day"
        valueLabel="Messages"
        showTable="Show as table"
        showChart="Show as chart"
        emptyText="Nothing yet."
      />,
    );
    expect(screen.getByText("Nothing yet.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names a payment's outcome in words", () => {
    const { container } = render(
      <PaymentStatusPanel tone="danger" title="The payment did not go through" />,
    );
    expect(screen.getByText("The payment did not go through")).toBeTruthy();
    expect(container.querySelector('[data-payment-panel="danger"]')).toBeTruthy();
    expect(container.querySelector("[data-spinner]")).toBeNull();
  });

  it("an invitation says who sent it and when (FEB-44), as text", () => {
    const { container } = render(
      <ul>
        <InvitationRow
          email="invitee@example.test"
          role={<RoleBadge tone="member">Member</RoleBadge>}
          state="Expires in 3 days"
          expired={false}
          stateFull={null}
          sender={`Invited by ${MARKUP}`}
          sent="Sent 2 days ago"
          sentFull="18 November 2026, 10:00"
        />
      </ul>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[data-invitation-sender]")?.textContent).toContain(MARKUP);
    expect(screen.getByText("Sent 2 days ago")).toBeTruthy();
  });
});
