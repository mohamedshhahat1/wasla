"use client";

import { Check, ChevronsUpDown } from "lucide-react";
import { DropdownMenu as MenuPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { focusRing, itemBase, popupSurface } from "../primitives/styles";

export interface SwitcherWorkspace {
  value: string;
  name: string;
  /** The person's role there, in words. */
  detail?: ReactNode;
  /** A status badge (a suspended workspace), drawn after the name. */
  badge?: ReactNode;
}

export interface SwitcherMenuProps {
  /** The trigger's accessible name prefix ("Workspace"); the current name follows it. */
  label: string;
  current: SwitcherWorkspace | null;
  workspaces: SwitcherWorkspace[];
  onSelect: (value: string) => void;
  /** Entries under the list (create a workspace, see all). */
  actions?: Array<{ key: string; label: ReactNode; onSelect: () => void }>;
  disabled?: boolean;
  /** Stories and the keyboard suite open it at once. */
  defaultOpen?: boolean;
}

/**
 * The visual part of the workspace switcher (FE-3, SH-05) on FE-1's non-modal menu (FEF-01): the
 * workspaces as radio items (aria-checked marks the current one), each with its role and status
 * badge, then the actions. The app wires the switch.
 */
export function SwitcherMenu({
  label,
  current,
  workspaces,
  onSelect,
  actions = [],
  disabled = false,
  defaultOpen,
}: SwitcherMenuProps) {
  return (
    <MenuPrimitive.Root modal={false} {...(defaultOpen === undefined ? {} : { defaultOpen })}>
      <MenuPrimitive.Trigger asChild disabled={disabled}>
        <button
          type="button"
          data-switcher-trigger=""
          className={cn(
            "wasla-press inline-flex min-h-target max-w-full cursor-pointer items-center gap-2 rounded-md",
            "border border-on-brand-muted bg-transparent px-3 text-md font-medium text-on-brand",
            "hover:bg-primary active:bg-primary-pressed disabled:cursor-not-allowed",
            focusRing,
          )}
        >
          <span className="sr-only">{label}: </span>
          <bdi className="truncate">{current?.name ?? ""}</bdi>
          {current?.badge ? <span className="hidden sm:inline-flex">{current.badge}</span> : null}
          <Icon icon={ChevronsUpDown} name="ChevronsUpDown" size={16} />
        </button>
      </MenuPrimitive.Trigger>
      <MenuPrimitive.Portal>
        <MenuPrimitive.Content
          align="start"
          sideOffset={4}
          className={cn(popupSurface, "max-w-prose")}
          data-switcher-menu=""
        >
          <MenuPrimitive.Label className="px-4 py-2 text-sm text-text-muted">
            {label}
          </MenuPrimitive.Label>
          <MenuPrimitive.RadioGroup
            value={current?.value ?? ""}
            onValueChange={(value) => {
              if (value !== current?.value) onSelect(value);
            }}
          >
            {workspaces.map((workspace) => (
              <MenuPrimitive.RadioItem
                key={workspace.value}
                value={workspace.value}
                className={cn(itemBase, "py-2")}
                data-workspace={workspace.value}
              >
                <span className="inline-flex size-5 shrink-0 items-center justify-center">
                  <MenuPrimitive.ItemIndicator>
                    <Icon icon={Check} name="Check" size={16} />
                  </MenuPrimitive.ItemIndicator>
                </span>
                <span className="flex min-w-0 flex-1 flex-col">
                  <bdi className="truncate">{workspace.name}</bdi>
                  {workspace.detail ? (
                    <span className="text-sm text-text-muted">{workspace.detail}</span>
                  ) : null}
                </span>
                {workspace.badge}
              </MenuPrimitive.RadioItem>
            ))}
          </MenuPrimitive.RadioGroup>
          {actions.length ? <MenuPrimitive.Separator className="mx-2 my-2 h-px bg-border" /> : null}
          {actions.map((action) => (
            <MenuPrimitive.Item key={action.key} className={itemBase} onSelect={action.onSelect}>
              {action.label}
            </MenuPrimitive.Item>
          ))}
        </MenuPrimitive.Content>
      </MenuPrimitive.Portal>
    </MenuPrimitive.Root>
  );
}
