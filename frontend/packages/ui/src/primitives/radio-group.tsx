"use client";

import { RadioGroup as RadioPrimitive } from "radix-ui";
import type { ComponentProps, KeyboardEvent, ReactNode } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

export interface RadioOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
}

export interface RadioGroupProps extends Omit<
  ComponentProps<typeof RadioPrimitive.Root>,
  "children"
> {
  options: RadioOption[];
}

/**
 * Shift+Tab out of the group. Radix makes its container focusable (so Tab can enter a group with no
 * tab stop yet) and takes it out of the order on Shift+Tab through a state update - which lands
 * after the browser has chosen where focus goes. Firefox chooses the container, the container hands
 * focus back to the checked radio, and the keyboard is trapped (KY-02 on GitHub, FE-6). Taking the
 * container out on the element itself, before the browser moves, lets focus leave backwards in
 * every browser; Radix's own render restores the value once focus has left.
 */
function leaveBackwards(event: KeyboardEvent<HTMLDivElement>): void {
  if (event.key === "Tab" && event.shiftKey) event.currentTarget.tabIndex = -1;
}

export function RadioGroup({ options, className, onKeyDownCapture, ...props }: RadioGroupProps) {
  return (
    <RadioPrimitive.Root
      className={cn("flex flex-col gap-2", className)}
      onKeyDownCapture={(event) => {
        leaveBackwards(event);
        onKeyDownCapture?.(event);
      }}
      {...props}
    >
      {options.map((option) => {
        const id = `${props.name ?? "radio"}-${option.value}`;
        return (
          <div key={option.value} className="flex items-center gap-2">
            <RadioPrimitive.Item
              id={id}
              value={option.value}
              disabled={option.disabled ?? false}
              className={cn(
                "group inline-flex size-target shrink-0 cursor-pointer items-center justify-center rounded-full",
                "disabled:cursor-not-allowed",
                focusRing,
              )}
            >
              <span className="inline-flex size-6 items-center justify-center rounded-full border-2 border-border-strong bg-surface transition-colors group-hover:border-primary group-data-[state=checked]:border-primary group-disabled:border-border group-disabled:bg-surface-sunken">
                <RadioPrimitive.Indicator className="size-3 rounded-full bg-primary" />
              </span>
            </RadioPrimitive.Item>
            <label htmlFor={id} className="text-md text-text">
              {option.label}
            </label>
          </div>
        );
      })}
    </RadioPrimitive.Root>
  );
}
