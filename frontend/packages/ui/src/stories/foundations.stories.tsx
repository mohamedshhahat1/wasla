import type { Meta, StoryObj } from "@storybook/react-vite";
import type { ReactNode } from "react";

import { Button } from "../primitives/button";
import { Link } from "../primitives/link";
import { labelsOf, localeOf } from "./helpers";

/**
 * Foundations (FE-RD, RS-02): the colour roles, type, space, shape and motion of DESIGN.md, drawn
 * from the role tokens themselves - a token change shows here first.
 */
const meta: Meta = { title: "Foundations" };
export default meta;
type Story = StoryObj;

function Swatch({ name, className, note }: { name: string; className: string; note: string }) {
  return (
    <li className="flex items-center gap-4">
      <span className={`size-10 shrink-0 rounded-md border border-border ${className}`} />
      <span className="flex flex-col">
        <code className="text-sm font-medium text-text">{name}</code>
        <span className="text-sm text-text-muted">{note}</span>
      </span>
    </li>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="m-0 text-lg font-semibold text-text">{title}</h2>
      <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-2">{children}</ul>
    </section>
  );
}

export const ColourRoles: Story = {
  render: () => (
    <div className="flex flex-col gap-8 bg-surface p-5">
      <h1 className="m-0 text-2xl font-semibold text-text">60 / 30 / 10</h1>
      <div
        className="flex h-16 w-full overflow-hidden rounded-lg border border-border"
        aria-hidden="true"
      >
        <span className="w-3/5 bg-canvas" />
        <span className="w-1/5 bg-secondary-subtle" />
        <span className="w-1/10 bg-brand" />
        <span className="w-1/10 bg-primary" />
      </div>
      <Group title="Dominant - about 60 %">
        <Swatch name="surface" className="bg-surface" note="cards, forms, dialogs" />
        <Swatch name="canvas" className="bg-canvas" note="the page behind cards" />
      </Group>
      <Group title="Secondary - about 30 %">
        <Swatch
          name="secondary-subtle"
          className="bg-secondary-subtle"
          note="panels, chips, hover"
        />
        <Swatch name="border-strong" className="bg-border-strong" note="field boundaries (3:1)" />
        <Swatch name="brand" className="bg-brand" note="the brand panel and bands" />
        <Swatch name="console" className="bg-console" note="the console's own band" />
      </Group>
      <Group title="Accent - about 10 %">
        <Swatch name="primary" className="bg-primary" note="the one main action" />
        <Swatch name="primary-hover" className="bg-primary-hover" note="hover" />
        <Swatch name="primary-pressed" className="bg-primary-pressed" note="pressed" />
        <Swatch name="focus" className="bg-focus" note="the focus ring" />
      </Group>
      <Group title="Status - for a status only">
        <Swatch name="danger" className="bg-danger" note="errors, destructive actions" />
        <Swatch name="warning" className="bg-warning" note="warnings" />
        <Swatch name="success" className="bg-success" note="success" />
        <Swatch name="info" className="bg-info" note="information is blue" />
      </Group>
    </div>
  ),
};

export const TypeScale: Story = {
  render: (_, ctx) => {
    const sample =
      localeOf(ctx) === "ar" ? "خدمة العملاء على كل قناة" : "Customer service on every channel";
    return (
      <div className="flex flex-col gap-4 bg-surface p-5">
        <h1 className="m-0 text-4xl font-semibold leading-tight tracking-tight text-text">
          {sample}
        </h1>
        <p className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
          {sample}
        </p>
        <p className="m-0 text-2xl font-semibold leading-tight text-text">{sample}</p>
        <p className="m-0 text-xl font-medium text-text">{sample}</p>
        <p className="m-0 text-lg text-text">{sample}</p>
        <p className="m-0 text-md text-text">{sample}</p>
        <p className="m-0 text-sm text-text-muted">{sample}</p>
        <p className="m-0 text-xs text-text-muted">{sample}</p>
      </div>
    );
  },
};

export const SpaceRadiusElevation: Story = {
  render: () => (
    <div className="flex flex-col gap-8 bg-canvas p-5">
      <h1 className="m-0 text-2xl font-semibold text-text">Space, radius, elevation</h1>
      <div className="flex flex-wrap items-end gap-3" aria-hidden="true">
        {[
          "size-1",
          "size-2",
          "size-3",
          "size-4",
          "size-5",
          "size-7",
          "size-8",
          "size-10",
          "size-11",
          "size-12",
        ].map((size) => (
          <span key={size} className={`${size} rounded-sm bg-primary`} />
        ))}
      </div>
      <div className="flex flex-wrap gap-5">
        <span className="flex size-12 items-center justify-center rounded-sm border border-border bg-surface text-sm text-text">
          6
        </span>
        <span className="flex size-12 items-center justify-center rounded-md border border-border bg-surface text-sm text-text">
          8
        </span>
        <span className="flex size-12 items-center justify-center rounded-lg border border-border bg-surface text-sm text-text">
          12
        </span>
        <span className="flex size-12 items-center justify-center rounded-xl border border-border bg-surface text-sm text-text">
          16
        </span>
      </div>
      <div className="flex flex-wrap gap-5">
        <span className="flex size-12 items-center justify-center rounded-lg bg-surface text-sm text-text shadow-sm">
          sm
        </span>
        <span className="flex size-12 items-center justify-center rounded-lg bg-surface text-sm text-text shadow-md">
          md
        </span>
        <span className="flex size-12 items-center justify-center rounded-lg bg-surface text-sm text-text shadow-lg">
          lg
        </span>
      </div>
    </div>
  ),
};

export const Motion: Story = {
  render: (_, ctx) => (
    <div className="flex flex-col gap-4 bg-surface p-5">
      <h1 className="m-0 text-2xl font-semibold text-text">Motion</h1>
      <p className="m-0 max-w-prose text-md text-text-muted">
        Press 120 ms, exits and hovers 150 ms, popovers and menus 200 ms, dialogs 240 ms, a strong
        ease-out; nothing over 300 ms. Under reduced motion everything is still: press the button.
      </p>
      <div>
        <Button>{labelsOf(ctx).save}</Button>
      </div>
    </div>
  ),
};

const VARIANTS = ["primary", "secondary", "ghost", "danger"] as const;

/** CM-01: every variant in every state a story can hold (hover and press are live). */
export const ButtonsMatrix: Story = {
  render: (_, ctx) => {
    const label = labelsOf(ctx).save;
    return (
      <table className="border-separate border-spacing-3 bg-surface text-start">
        <caption className="pb-3 text-start text-md font-semibold text-text">Buttons</caption>
        <thead>
          <tr>
            <th scope="col" className="text-start text-sm font-medium text-text-muted">
              variant
            </th>
            {["rest", "disabled", "busy"].map((state) => (
              <th
                key={state}
                scope="col"
                className="text-start text-sm font-medium text-text-muted"
              >
                {state}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {VARIANTS.map((variant) => (
            <tr key={variant}>
              <th scope="row" className="text-start text-sm font-medium text-text">
                {variant}
              </th>
              <td>
                <Button variant={variant}>{label}</Button>
              </td>
              <td>
                <Button variant={variant} disabled>
                  {label}
                </Button>
              </td>
              <td>
                <Button variant={variant} busy>
                  {label}
                </Button>
              </td>
            </tr>
          ))}
          <tr>
            <th scope="row" className="text-start text-sm font-medium text-text">
              link
            </th>
            <td colSpan={3}>
              <Link href="#terms">{label}</Link>
            </td>
          </tr>
        </tbody>
      </table>
    );
  },
};
