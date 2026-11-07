import type { Meta, StoryObj } from "@storybook/react-vite";

import { EmptyState, ErrorState } from "../composites/states";
import { Button } from "../primitives/button";
import { LONG_ARABIC, LONG_TURKISH } from "./samples";

const meta: Meta = { title: "Composites/States" };
export default meta;
type Story = StoryObj;

export const Empty: Story = {
  render: () => (
    <EmptyState
      title="No conversations yet"
      description={LONG_TURKISH}
      action={<Button>Connect a channel</Button>}
    />
  ),
};
export const Error: Story = {
  render: () => (
    <ErrorState
      title="Something went wrong"
      description={LONG_ARABIC}
      requestId="7f2c1a9e-4b5d-4c3e-9f10-2a6b8d0e1c34"
      requestIdLabel="Reference:"
      action={<Button variant="secondary">Try again</Button>}
    />
  ),
};
export const ErrorWithoutRequestId: Story = {
  render: () => <ErrorState title="Page not found" description="Check the address." />,
};
