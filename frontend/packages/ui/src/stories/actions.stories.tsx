import type { Meta, StoryObj } from "@storybook/react-vite";
import { ArrowRight, MoreHorizontal, Send } from "lucide-react";

import { Icon } from "../icon";
import { Button } from "../primitives/button";
import { IconButton } from "../primitives/icon-button";
import { Link } from "../primitives/link";
import { labelsOf } from "./helpers";
import { LONG_ARABIC, LONG_TURKISH, MIXED_NAME } from "./samples";

const meta: Meta = { title: "Primitives/Button" };
export default meta;
type Story = StoryObj;

export const Default: Story = {
  render: (_, ctx) => (
    <div className="flex flex-wrap gap-3">
      <Button>{labelsOf(ctx).save}</Button>
      <Button variant="secondary">{labelsOf(ctx).cancel}</Button>
      <Button variant="ghost">{labelsOf(ctx).open}</Button>
      <Button variant="danger">Delete</Button>
      <Button>
        {labelsOf(ctx).open} <Icon icon={ArrowRight} name="ArrowRight" />
      </Button>
    </div>
  ),
};
export const Disabled: Story = {
  render: (_, ctx) => <Button disabled>{labelsOf(ctx).save}</Button>,
};
export const LongArabic: Story = { render: () => <Button>{LONG_ARABIC}</Button> };
export const LongTurkish: Story = { render: () => <Button>{LONG_TURKISH}</Button> };
export const MixedDirection: Story = {
  render: () => (
    <Button>
      Reply to <bdi>{MIXED_NAME}</bdi>
    </Button>
  ),
};

export const IconButtons: Story = {
  render: (_, ctx) => (
    <div className="flex gap-3">
      <IconButton icon={MoreHorizontal} iconName="MoreHorizontal" label={labelsOf(ctx).more} />
      <IconButton icon={Send} iconName="Send" label="Send" />
      <IconButton icon={Send} iconName="Send" label="Send" disabled />
    </div>
  ),
};

export const Links: Story = {
  render: () => (
    <p className="text-md">
      <Link href="#terms">Terms</Link> · <Link href="#ar">{LONG_ARABIC}</Link> ·{" "}
      <Link href="#tr">{LONG_TURKISH}</Link> ·{" "}
      <Link href="#mixed">
        <bdi>{MIXED_NAME}</bdi>
      </Link>
    </p>
  ),
};
