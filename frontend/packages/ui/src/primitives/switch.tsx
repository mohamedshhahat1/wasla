"use client";

import { Switch as SwitchPrimitive } from "radix-ui";
import type { ComponentProps } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

export type SwitchProps = ComponentProps<typeof SwitchPrimitive.Root>;

/** The thumb moves towards the inline end when on, in both directions. */
export function Switch({ className, ...props }: SwitchProps) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "group inline-flex min-h-target min-w-target shrink-0 items-center rounded-full",
        "disabled:cursor-not-allowed disabled:opacity-60",
        focusRing,
        className,
      )}
      {...props}
    >
      <span className="flex h-7 w-10 items-center rounded-full bg-border-strong p-1 transition-colors group-data-[state=checked]:justify-end group-data-[state=checked]:bg-primary">
        <SwitchPrimitive.Thumb className="block size-5 rounded-full bg-surface shadow-sm" />
      </span>
    </SwitchPrimitive.Root>
  );
}
