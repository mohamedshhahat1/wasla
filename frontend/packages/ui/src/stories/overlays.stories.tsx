import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { FormField } from "../composites/form-field";
import { Button } from "../primitives/button";
import { ConfirmDialog, Dialog } from "../primitives/dialog";
import { Input } from "../primitives/input";
import { DropdownMenu } from "../primitives/menu";
import { Popover, Tooltip } from "../primitives/overlays";
import { Tabs } from "../primitives/tabs";
import { Toast, ToastViewport } from "../primitives/toast";
import { dirOf, labelsOf } from "./helpers";
import { LONG_ARABIC, LONG_TURKISH, MIXED_NAME } from "./samples";

const meta: Meta = { title: "Primitives/Overlays" };
export default meta;
type Story = StoryObj;

export const DialogDefault: Story = {
  name: "Dialog",
  render: (_, ctx) => (
    <Dialog
      trigger={<Button data-testid="trigger">Open dialog</Button>}
      title="Rename workspace"
      description={LONG_TURKISH}
      closeLabel={labelsOf(ctx).close}
      footer={<Button>{labelsOf(ctx).save}</Button>}
    >
      <FormField label="Name">
        {(control) => <Input defaultValue={MIXED_NAME} {...control} />}
      </FormField>
    </Dialog>
  ),
};
export const DialogOpenArabic: Story = {
  render: (_, ctx) => (
    <Dialog
      defaultOpen
      title={LONG_ARABIC}
      description={LONG_ARABIC}
      closeLabel={labelsOf(ctx).close}
    />
  ),
};

export const ConfirmDialogTyped: Story = {
  render: (_, ctx) => (
    <ConfirmDialog
      defaultOpen
      title="Delete workspace"
      description="This cannot be undone."
      closeLabel={labelsOf(ctx).close}
      confirmationText="acme"
      confirmationLabel={
        <>
          Type <bdi>acme</bdi> to confirm
        </>
      }
      confirmLabel="Delete"
      cancelLabel={labelsOf(ctx).cancel}
      onConfirm={() => undefined}
    />
  ),
};

export const DropdownMenuDefault: Story = {
  name: "DropdownMenu",
  render: () => (
    <DropdownMenu
      trigger={<Button data-testid="trigger">Menu</Button>}
      items={[
        { type: "item", key: "a", label: "Alpha" },
        { type: "item", key: "b", label: "Beta" },
        { type: "separator", key: "s" },
        {
          type: "submenu",
          key: "more",
          label: "More",
          items: [
            { type: "item", key: "c", label: "Gamma" },
            { type: "item", key: "d", label: "Delta" },
          ],
        },
        { type: "item", key: "x", label: "Disabled", disabled: true },
      ]}
    />
  ),
};
export const DropdownMenuText: Story = {
  render: () => (
    <DropdownMenu
      defaultOpen
      trigger={<Button>Menu</Button>}
      items={[
        { type: "item", key: "ar", label: LONG_ARABIC },
        { type: "item", key: "tr", label: LONG_TURKISH },
        { type: "item", key: "mixed", label: <bdi>{MIXED_NAME}</bdi> },
      ]}
    />
  ),
};

export const PopoverDefault: Story = {
  name: "Popover",
  render: (_, ctx) => (
    <div className="flex justify-center">
      <Popover
        dir={dirOf(ctx)}
        side="inline-end"
        label="Details"
        trigger={<Button data-testid="trigger">Details</Button>}
      >
        <p className="m-0">{LONG_TURKISH}</p>
        <Button className="mt-3" data-testid="inside">
          {labelsOf(ctx).save}
        </Button>
      </Popover>
    </div>
  ),
};
export const PopoverOpenArabic: Story = {
  render: (_, ctx) => (
    <Popover dir={dirOf(ctx)} defaultOpen label="Details" trigger={<Button>Details</Button>}>
      <p className="m-0">{LONG_ARABIC}</p>
    </Popover>
  ),
};

export const TooltipDefault: Story = {
  name: "Tooltip",
  render: () => (
    <Tooltip defaultOpen content={LONG_ARABIC}>
      <Button variant="secondary">Hover or focus</Button>
    </Tooltip>
  ),
};
export const TooltipTurkish: Story = {
  render: () => (
    <Tooltip defaultOpen content={LONG_TURKISH}>
      <Button variant="secondary">
        <bdi>{MIXED_NAME}</bdi>
      </Button>
    </Tooltip>
  ),
};

export const TabsDefault: Story = {
  name: "Tabs",
  render: () => (
    <Tabs
      label="Sections"
      items={[
        { value: "first", label: "First", content: <p>First panel</p> },
        { value: "second", label: "Second", content: <p>Second panel</p> },
        { value: "third", label: "Third", content: <p>Third panel</p> },
      ]}
    />
  ),
};
export const TabsText: Story = {
  render: () => (
    <Tabs
      label="Text"
      items={[
        { value: "ar", label: "العربية", content: <p>{LONG_ARABIC}</p> },
        { value: "tr", label: "Türkçe İ ı", content: <p>{LONG_TURKISH}</p> },
        { value: "mixed", label: <bdi>{MIXED_NAME}</bdi>, content: <p>mixed</p> },
        { value: "off", label: "Disabled", content: null, disabled: true },
      ]}
    />
  ),
};

function ToastStory({ closeLabel }: { closeLabel: string }) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Show</Button>
      <Toast
        open={open}
        onOpenChange={setOpen}
        tone="success"
        title="Saved"
        description={LONG_ARABIC}
        closeLabel={closeLabel}
      />
      <Toast
        open={open}
        onOpenChange={setOpen}
        tone="danger"
        title={LONG_TURKISH}
        closeLabel={closeLabel}
      />
      <ToastViewport label="Notifications" />
    </>
  );
}
export const ToastDefault: Story = {
  name: "Toast",
  render: (_, ctx) => <ToastStory closeLabel={labelsOf(ctx).close} />,
};
