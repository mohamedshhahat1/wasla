import { Slot } from "radix-ui";
import type { AnchorHTMLAttributes } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

export interface LinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  /** Render the child (a framework link) with the link's look. */
  asChild?: boolean;
}

export const linkClasses = (className?: string) =>
  cn(
    "rounded-sm font-medium text-primary underline decoration-1 underline-offset-4",
    "transition-colors hover:text-primary-hover hover:decoration-2 focus-visible:decoration-2",
    focusRing,
    className,
  );

export function Link({ asChild = false, className, children, ...props }: LinkProps) {
  if (asChild) {
    return (
      <Slot.Root className={linkClasses(className)} {...props}>
        {children}
      </Slot.Root>
    );
  }
  return (
    <a className={linkClasses(className)} {...props}>
      {children}
    </a>
  );
}
