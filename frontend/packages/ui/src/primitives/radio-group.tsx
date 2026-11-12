"use client";

import { RadioGroup as RadioPrimitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

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

export function RadioGroup({ options, className, ...props }: RadioGroupProps) {
  return (
    <RadioPrimitive.Root className={cn("flex flex-col gap-2", className)} {...props}>
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
