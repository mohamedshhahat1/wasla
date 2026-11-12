import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { focusRing } from "./styles";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: LucideIcon;
  iconName: string;
  /** Required: an icon button has no visible text, so this is its accessible name. */
  label: string;
}

export function IconButton({ icon, iconName, label, className, type, ...props }: IconButtonProps) {
  return (
    <button
      type={type ?? "button"}
      aria-label={label}
      title={label}
      className={cn(
        "wasla-press inline-flex min-h-target min-w-target cursor-pointer items-center",
        "justify-center rounded-md border border-transparent text-text",
        "hover:bg-secondary-subtle hover:text-primary active:bg-secondary-subtle",
        "disabled:cursor-not-allowed disabled:text-text-muted disabled:hover:bg-transparent",
        focusRing,
        className,
      )}
      {...props}
    >
      <Icon icon={icon} name={iconName} />
    </button>
  );
}
