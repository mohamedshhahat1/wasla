import { Slot } from "radix-ui";
import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

/**
 * FE-1's four variants, redrawn (CM-01):
 * - primary: the action blue, the 10 % accent - one per view where possible;
 * - secondary: white with a blue outline and blue-950 text, light blue on hover;
 * - ghost: the quiet button - no fill, blue text, the canvas blue on hover;
 * - danger: the status red, for destructive actions only.
 */
export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<ButtonVariant, string> = {
  primary:
    "border border-primary bg-primary text-on-primary shadow-sm " +
    "hover:border-primary-hover hover:bg-primary-hover " +
    "active:border-primary-pressed active:bg-primary-pressed",
  secondary:
    "border border-border-strong bg-surface text-text shadow-sm " +
    "hover:border-primary hover:bg-secondary-subtle active:bg-secondary-subtle",
  ghost:
    "border border-transparent bg-transparent text-primary " +
    "hover:bg-secondary-subtle hover:text-primary-hover active:bg-secondary-subtle",
  danger:
    "border border-danger bg-danger text-text-inverse shadow-sm hover:opacity-90 active:opacity-100",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Render the child element (a link, say) with the button's look and behaviour. */
  asChild?: boolean;
  /**
   * The submit in flight: disabled, `aria-busy`, and a turning ring before the label. The label
   * stays, so the button's name does not change while it works.
   */
  busy?: boolean;
}

export const buttonClasses = (variant: ButtonVariant = "primary", className?: string) =>
  cn(
    "wasla-press inline-flex min-h-target min-w-target cursor-pointer select-none items-center",
    "justify-center gap-3 rounded-md px-6 text-md font-medium leading-tight",
    "disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-sunken",
    "disabled:text-text-muted disabled:shadow-none disabled:opacity-100",
    "aria-busy:cursor-progress",
    focusRing,
    variants[variant],
    className,
  );

/** The busy ring, drawn in CSS: a circle whose one quarter is transparent, turning. */
export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-spinner=""
      className={cn(
        "wasla-spinner inline-block size-5 shrink-0 rounded-full border-2 border-current border-e-transparent",
        className,
      )}
    />
  );
}

export function Button({
  variant = "primary",
  asChild = false,
  busy = false,
  className,
  type,
  disabled,
  children,
  ...props
}: ButtonProps) {
  if (asChild) {
    return (
      <Slot.Root className={buttonClasses(variant, className)} {...props}>
        {children}
      </Slot.Root>
    );
  }
  const content: ReactNode = busy ? (
    <>
      <Spinner />
      {children}
    </>
  ) : (
    children
  );
  return (
    <button
      type={type ?? "button"}
      className={buttonClasses(variant, className)}
      disabled={disabled || busy}
      {...(busy ? { "aria-busy": true } : {})}
      {...props}
    >
      {content}
    </button>
  );
}
