import type { LucideIcon, LucideProps } from "lucide-react";

import { cn } from "./cn";

/**
 * Icons that point along the reading direction and therefore mirror in RTL (DS-03). Logos,
 * media controls, checkmarks and clocks never flip.
 */
export const FLIP_IN_RTL = new Set([
  "ArrowLeft",
  "ArrowRight",
  "ChevronLeft",
  "ChevronRight",
  "ChevronsLeft",
  "ChevronsRight",
  "Send",
  "SendHorizontal",
  "Reply",
  "ReplyAll",
  "Undo",
  "Undo2",
  "Redo",
  "Redo2",
  "LogOut",
  "LogIn",
]);

/** Icons that must never mirror, even if one day added to the list above by mistake. */
export const NEVER_FLIP = new Set(["Play", "Pause", "Check", "CheckCheck", "Clock", "Clock3"]);

export function shouldFlip(name: string): boolean {
  return FLIP_IN_RTL.has(name) && !NEVER_FLIP.has(name);
}

export interface IconProps extends Omit<LucideProps, "ref"> {
  icon: LucideIcon;
  /** The icon's name as lucide exports it (displayName is not kept by every bundler). */
  name: string;
  /** A label makes the icon meaningful; without one it is decorative and hidden. */
  label?: string;
}

/** The one icon wrapper: decorative by default, mirrored in RTL when its meaning is a direction. */
export function Icon({ icon: Component, name, label, className, ...props }: IconProps) {
  return (
    <Component
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      focusable="false"
      data-icon={name}
      className={cn("inline-block shrink-0", shouldFlip(name) && "wasla-flip-rtl", className)}
      size={20}
      strokeWidth={1.75}
      {...props}
    />
  );
}
