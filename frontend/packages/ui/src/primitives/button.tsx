import { Slot } from "radix-ui";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "../cn";
import { focusRing } from "./styles";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<ButtonVariant, string> = {
  primary: "border border-primary bg-primary text-on-primary hover:bg-primary-hover",
  secondary: "border border-border-strong bg-surface text-text hover:bg-surface-sunken",
  ghost: "border border-transparent bg-transparent text-text hover:bg-surface-sunken",
  danger: "border border-danger bg-danger text-text-inverse hover:opacity-90",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Render the child element (a link, say) with the button's look and behaviour. */
  asChild?: boolean;
}

export const buttonClasses = (variant: ButtonVariant = "primary", className?: string) =>
  cn(
    "inline-flex min-h-target min-w-target items-center justify-center gap-3 rounded-md px-5",
    "text-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60",
    focusRing,
    variants[variant],
    className,
  );

export function Button({
  variant = "primary",
  asChild = false,
  className,
  type,
  ...props
}: ButtonProps) {
  if (asChild) return <Slot.Root className={buttonClasses(variant, className)} {...props} />;
  return (
    <button type={type ?? "button"} className={buttonClasses(variant, className)} {...props} />
  );
}
