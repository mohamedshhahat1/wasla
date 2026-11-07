"use client";

import { Check } from "lucide-react";
import { Checkbox as CheckboxPrimitive } from "radix-ui";
import type { ComponentProps } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { focusRing } from "./styles";

export type CheckboxProps = ComponentProps<typeof CheckboxPrimitive.Root>;

/** A 40×40 target around a 20×20 box. Labelled by a <label htmlFor> beside it. */
export function Checkbox({ className, ...props }: CheckboxProps) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        "group inline-flex size-target shrink-0 items-center justify-center rounded-md",
        "disabled:cursor-not-allowed disabled:opacity-60",
        focusRing,
        className,
      )}
      {...props}
    >
      <span
        className={cn(
          "inline-flex size-6 items-center justify-center rounded-sm border border-border-strong",
          "bg-surface group-data-[state=checked]:border-primary group-data-[state=checked]:bg-primary",
          "group-aria-invalid:border-danger",
        )}
      >
        <CheckboxPrimitive.Indicator className="text-on-primary">
          <Icon icon={Check} name="Check" size={16} />
        </CheckboxPrimitive.Indicator>
      </span>
    </CheckboxPrimitive.Root>
  );
}
