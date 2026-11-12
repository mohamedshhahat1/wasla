"use client";

import { Tabs as TabsPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

export interface TabItem {
  value: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
}

export interface TabsProps {
  items: TabItem[];
  defaultValue?: string;
  /** The tab list's accessible name. */
  label: string;
}

/** Arrow keys follow the reading direction (Radix reads it from Direction.Provider). */
export function Tabs({ items, defaultValue, label }: TabsProps) {
  return (
    <TabsPrimitive.Root defaultValue={defaultValue ?? items[0]?.value ?? ""}>
      <TabsPrimitive.List aria-label={label} className="flex gap-1 border-b border-border">
        {items.map((item) => (
          <TabsPrimitive.Trigger
            key={item.value}
            value={item.value}
            disabled={item.disabled ?? false}
            className={cn(
              "-mb-px min-h-target cursor-pointer rounded-t-md border-b-2 border-transparent px-5 text-md",
              "text-text-muted transition-colors hover:bg-secondary-subtle hover:text-text",
              "data-[state=active]:border-primary data-[state=active]:font-medium data-[state=active]:text-primary",
              "disabled:cursor-not-allowed disabled:hover:bg-transparent",
              focusRing,
            )}
          >
            {item.label}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      {items.map((item) => (
        <TabsPrimitive.Content
          key={item.value}
          value={item.value}
          className={cn("pt-5", focusRing)}
        >
          {item.content}
        </TabsPrimitive.Content>
      ))}
    </TabsPrimitive.Root>
  );
}
