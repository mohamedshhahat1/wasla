"use client";

import { Popover as PopoverPrimitive, Tooltip as TooltipPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { popupSurface } from "./styles";

export type LogicalSide = "top" | "bottom" | "inline-start" | "inline-end";

/**
 * Radix's `side` is physical (left/right). Screens say inline-start / inline-end and this maps
 * them by the reading direction, so a popover "after" its trigger is on the right in English
 * and on the left in Arabic (DS-07 records what Radix itself does with `side` and `align`).
 */
export function physicalSide(
  side: LogicalSide,
  dir: "ltr" | "rtl",
): "top" | "bottom" | "left" | "right" {
  if (side === "top" || side === "bottom") return side;
  const start = dir === "rtl" ? "right" : "left";
  const end = dir === "rtl" ? "left" : "right";
  return side === "inline-start" ? start : end;
}

export interface PopoverProps {
  trigger: ReactNode;
  children: ReactNode;
  side?: LogicalSide;
  dir: "ltr" | "rtl";
  /** The popover's accessible name. */
  label: string;
  defaultOpen?: boolean;
}

export function Popover({
  trigger,
  children,
  side = "bottom",
  dir,
  label,
  defaultOpen,
}: PopoverProps) {
  return (
    <PopoverPrimitive.Root {...(defaultOpen === undefined ? {} : { defaultOpen })}>
      <PopoverPrimitive.Trigger asChild>{trigger}</PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          side={physicalSide(side, dir)}
          align="start"
          sideOffset={6}
          aria-label={label}
          className={cn(popupSurface, "max-w-prose p-5")}
        >
          {children}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

export interface TooltipProps {
  /** One focusable element: the tooltip opens on hover and on keyboard focus. */
  children: ReactNode;
  content: ReactNode;
  side?: "top" | "bottom";
  defaultOpen?: boolean;
}

export function Tooltip({ children, content, side = "top", defaultOpen }: TooltipProps) {
  return (
    <TooltipPrimitive.Root {...(defaultOpen === undefined ? {} : { defaultOpen })}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="wasla-pop wasla-tooltip z-(--z-toast) max-w-prose rounded-md bg-text px-3 py-2 text-sm text-text-inverse shadow-md"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
