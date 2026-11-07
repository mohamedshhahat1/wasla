"use client";

import { ChevronRight } from "lucide-react";
import { DropdownMenu as MenuPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";
import { itemBase, popupSurface } from "./styles";

export type MenuEntry =
  | { type: "item"; key: string; label: ReactNode; onSelect?: () => void; disabled?: boolean }
  | { type: "separator"; key: string }
  | { type: "submenu"; key: string; label: ReactNode; items: MenuEntry[] };

function Entries({ items }: { items: MenuEntry[] }) {
  return (
    <>
      {items.map((entry) => {
        if (entry.type === "separator") {
          return <MenuPrimitive.Separator key={entry.key} className="my-2 h-px bg-border" />;
        }
        if (entry.type === "submenu") {
          return (
            <MenuPrimitive.Sub key={entry.key}>
              <MenuPrimitive.SubTrigger className={cn(itemBase, "justify-between")}>
                {entry.label}
                <Icon icon={ChevronRight} name="ChevronRight" size={16} />
              </MenuPrimitive.SubTrigger>
              <MenuPrimitive.Portal>
                <MenuPrimitive.SubContent sideOffset={4} className={popupSurface}>
                  <Entries items={entry.items} />
                </MenuPrimitive.SubContent>
              </MenuPrimitive.Portal>
            </MenuPrimitive.Sub>
          );
        }
        return (
          <MenuPrimitive.Item
            key={entry.key}
            disabled={entry.disabled ?? false}
            className={itemBase}
            {...(entry.onSelect ? { onSelect: entry.onSelect } : {})}
          >
            {entry.label}
          </MenuPrimitive.Item>
        );
      })}
    </>
  );
}

export interface DropdownMenuProps {
  /** The trigger element (a Button, an IconButton). */
  trigger: ReactNode;
  items: MenuEntry[];
  defaultOpen?: boolean;
}

/**
 * Non-modal (FEF-01): Radix's modal menu marks the rest of the page aria-hidden while the
 * trigger stays focusable inside it (axe aria-hidden-focus, serious). Non-modal keeps the
 * WAI-ARIA menu keyboard model; a click or focus outside closes the menu.
 */
export function DropdownMenu({ trigger, items, defaultOpen }: DropdownMenuProps) {
  return (
    <MenuPrimitive.Root modal={false} {...(defaultOpen === undefined ? {} : { defaultOpen })}>
      <MenuPrimitive.Trigger asChild>{trigger}</MenuPrimitive.Trigger>
      <MenuPrimitive.Portal>
        <MenuPrimitive.Content align="start" sideOffset={4} className={popupSurface}>
          <Entries items={items} />
        </MenuPrimitive.Content>
      </MenuPrimitive.Portal>
    </MenuPrimitive.Root>
  );
}
