import { Slot } from "radix-ui";
import type { AnchorHTMLAttributes } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

/** `inverse` on the blue bands (the footer, the brand panel): white, underlined. */
export type LinkTone = "default" | "inverse";

export interface LinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  /** Render the child (a framework link) with the link's look. */
  asChild?: boolean;
  tone?: LinkTone;
}

const tones: Record<LinkTone, string> = {
  default: "text-primary hover:text-primary-hover",
  inverse: "text-on-brand",
};

export const linkClasses = (className?: string, tone: LinkTone = "default") =>
  cn(
    "rounded-sm font-medium underline decoration-1 underline-offset-4",
    "transition-colors hover:decoration-2 focus-visible:decoration-2",
    tones[tone],
    focusRing,
    className,
  );

export function Link({
  asChild = false,
  tone = "default",
  className,
  children,
  ...props
}: LinkProps) {
  if (asChild) {
    return (
      <Slot.Root className={linkClasses(className, tone)} {...props}>
        {children}
      </Slot.Root>
    );
  }
  return (
    <a className={linkClasses(className, tone)} {...props}>
      {children}
    </a>
  );
}
