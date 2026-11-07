import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { FormField } from "../composites/form-field";
import { Checkbox } from "../primitives/checkbox";
import { Combobox } from "../primitives/combobox";
import { Input, Textarea } from "../primitives/input";
import { RadioGroup } from "../primitives/radio-group";
import { Select } from "../primitives/select";
import { Switch } from "../primitives/switch";
import { localeOf } from "./helpers";
import { LONG_ARABIC, LONG_TURKISH, MIXED_NAME } from "./samples";

const meta: Meta = { title: "Primitives/Fields" };
export default meta;
type Story = StoryObj;

export const InputDefault: Story = {
  render: () => (
    <FormField label="Email" hint="We send a code to this address.">
      {(control) => <Input type="email" autoComplete="email" {...control} />}
    </FormField>
  ),
};
export const InputDisabled: Story = {
  render: () => (
    <FormField label="Workspace slug">
      {(control) => <Input disabled defaultValue="acme" {...control} />}
    </FormField>
  ),
};
export const InputInvalid: Story = {
  render: () => (
    <FormField label="Email" error="Enter a valid email address." required requiredLabel="required">
      {(control) => <Input type="email" defaultValue="not-an-email" {...control} />}
    </FormField>
  ),
};
export const InputLongArabic: Story = {
  render: () => (
    <FormField label={LONG_ARABIC}>
      {(control) => <Input defaultValue={LONG_ARABIC} {...control} />}
    </FormField>
  ),
};
export const InputLongTurkish: Story = {
  render: () => (
    <FormField label={LONG_TURKISH}>
      {(control) => <Input defaultValue={LONG_TURKISH} {...control} />}
    </FormField>
  ),
};
export const InputMixedDirection: Story = {
  render: () => (
    <FormField label="Customer">
      {(control) => <Input dir="auto" defaultValue={MIXED_NAME} {...control} />}
    </FormField>
  ),
};

export const TextareaDefault: Story = {
  render: () => (
    <FormField label="Note" hint={LONG_TURKISH}>
      {(control) => <Textarea defaultValue={LONG_ARABIC} {...control} />}
    </FormField>
  ),
};
export const TextareaDisabled: Story = {
  render: () => (
    <FormField label="Note">{(control) => <Textarea disabled {...control} />}</FormField>
  ),
};

export const CheckboxDefault: Story = {
  render: () => (
    <div className="flex flex-col gap-2">
      {[
        ["c1", "Email me about new messages", false],
        ["c2", LONG_ARABIC, true],
        ["c3", LONG_TURKISH, false],
      ].map(([id, label, checked]) => (
        <div key={String(id)} className="flex items-center gap-2">
          <Checkbox id={String(id)} defaultChecked={Boolean(checked)} />
          <label htmlFor={String(id)}>{label}</label>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <Checkbox id="c4" disabled />
        <label htmlFor="c4">Disabled</label>
      </div>
    </div>
  ),
};

const radioOptions = [
  { value: "first", label: "First" },
  { value: "second", label: "Second" },
  { value: "third", label: "Third" },
];
export const RadioGroupDefault: Story = {
  name: "RadioGroup",
  render: () => (
    <RadioGroup name="plan" aria-label="Plan" defaultValue="first" options={radioOptions} />
  ),
};
export const RadioGroupText: Story = {
  render: () => (
    <RadioGroup
      name="text"
      aria-label="Text"
      options={[
        { value: "ar", label: LONG_ARABIC },
        { value: "tr", label: LONG_TURKISH },
        { value: "mixed", label: <bdi>{MIXED_NAME}</bdi> },
        { value: "off", label: "Disabled", disabled: true },
      ]}
    />
  ),
};

export const SwitchDefault: Story = {
  render: () => (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        <Switch id="s1" defaultChecked />
        <label htmlFor="s1">AI replies</label>
      </div>
      <div className="flex items-center gap-3">
        <Switch id="s2" />
        <label htmlFor="s2">{LONG_ARABIC}</label>
      </div>
      <div className="flex items-center gap-3">
        <Switch id="s3" disabled />
        <label htmlFor="s3">{LONG_TURKISH}</label>
      </div>
    </div>
  ),
};

const selectOptions = [
  { value: "first", label: "First" },
  { value: "second", label: "Second" },
  { value: "third", label: "Third" },
];
export const SelectDefault: Story = {
  name: "Select",
  render: () => (
    <FormField label="Choice">
      {(control) => <Select {...control} options={selectOptions} placeholder="Choose…" />}
    </FormField>
  ),
};
export const SelectInvalid: Story = {
  render: () => (
    <FormField label="Language" error="Choose a language.">
      {(control) => (
        <Select
          {...control}
          placeholder="Choose…"
          options={[
            { value: "ar", label: "العربية" },
            { value: "tr", label: "Türkçe — İzmir" },
            { value: "x", label: "Disabled", disabled: true },
          ]}
        />
      )}
    </FormField>
  ),
};

function ComboboxStory({ locale, invalid }: { locale: string; invalid?: boolean }) {
  const [value, setValue] = useState<string | null>(null);
  return (
    <FormField label="City" {...(invalid ? { error: "Choose a city." } : {})}>
      {(control) => (
        <Combobox
          {...control}
          options={[
            { value: "ist", label: "İstanbul" },
            { value: "izm", label: "İzmir" },
            { value: "cai", label: "القاهرة" },
            { value: "alx", label: "Alexandria" },
            { value: "par", label: "Paris" },
          ]}
          value={value}
          onValueChange={setValue}
          placeholder={locale === "ar" ? "ابحث…" : "Search…"}
          emptyText={locale === "ar" ? "لا توجد نتائج" : "No results"}
        />
      )}
    </FormField>
  );
}
export const ComboboxDefault: Story = {
  name: "Combobox",
  render: (_, ctx) => <ComboboxStory locale={localeOf(ctx)} />,
};
export const ComboboxInvalid: Story = {
  render: (_, ctx) => <ComboboxStory locale={localeOf(ctx)} invalid />,
};
