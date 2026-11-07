"use client";

import { Popover as PopoverPrimitive } from "radix-ui";
import { useId, useMemo, useState, type KeyboardEvent } from "react";

import { cn } from "../cn";
import { useUiLocale } from "../provider";
import { controlBase, itemBase, popupSurface } from "./styles";

export interface ComboboxOption {
  value: string;
  label: string;
}

export interface ComboboxProps {
  options: ComboboxOption[];
  value: string | null;
  onValueChange: (value: string | null) => void;
  placeholder?: string;
  /** Shown as the only row when nothing matches. */
  emptyText: string;
  id?: string;
  disabled?: boolean;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}

/**
 * A searchable select (DS-04): Radix has no combobox, so this is the WAI-ARIA 1.2 combobox
 * pattern over Radix Popover - an input with role="combobox", a listbox, aria-activedescendant,
 * focus kept on the input. Matching folds case with the UI locale (Turkish İ/ı, plan section 20).
 */
export function Combobox({
  options,
  value,
  onValueChange,
  placeholder,
  emptyText,
  id,
  disabled,
  "aria-labelledby": labelledBy,
  "aria-describedby": describedBy,
  "aria-invalid": invalid,
}: ComboboxProps) {
  const locale = useUiLocale();
  const listId = useId();
  const optionId = (index: number) => `${listId}-option-${index}`;
  const selected = options.find((option) => option.value === value) ?? null;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(selected?.label ?? "");
  const [active, setActive] = useState(0);

  const matches = useMemo(() => {
    const folded = query.trim().toLocaleLowerCase(locale);
    if (!folded || folded === selected?.label.toLocaleLowerCase(locale)) return options;
    return options.filter((option) => option.label.toLocaleLowerCase(locale).includes(folded));
  }, [options, query, locale, selected]);

  const choose = (option: ComboboxOption) => {
    onValueChange(option.value);
    setQuery(option.label);
    setOpen(false);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) {
          setOpen(true);
          setActive(0);
        } else {
          setActive((index) => Math.min(index + 1, Math.max(matches.length - 1, 0)));
        }
        break;
      case "ArrowUp":
        event.preventDefault();
        if (open) setActive((index) => Math.max(index - 1, 0));
        break;
      case "Enter": {
        const option = matches[active];
        if (open && option) {
          event.preventDefault();
          choose(option);
        }
        break;
      }
      case "Escape":
        if (open) {
          event.preventDefault();
          setOpen(false);
        } else if (query) {
          setQuery("");
          onValueChange(null);
        }
        break;
      default:
        break;
    }
  };

  const activeOption = open && matches[active] ? optionId(active) : undefined;

  return (
    <PopoverPrimitive.Root open={open && !disabled} onOpenChange={setOpen}>
      <PopoverPrimitive.Anchor asChild>
        <input
          id={id}
          type="text"
          role="combobox"
          autoComplete="off"
          aria-autocomplete="list"
          aria-expanded={open}
          aria-controls={listId}
          aria-activedescendant={activeOption}
          aria-labelledby={labelledBy}
          aria-describedby={describedBy}
          aria-invalid={invalid}
          disabled={disabled}
          placeholder={placeholder}
          value={query}
          className={cn(controlBase)}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
            setOpen(true);
          }}
          onClick={() => setOpen(true)}
          onKeyDown={onKeyDown}
        />
      </PopoverPrimitive.Anchor>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align="start"
          sideOffset={4}
          className={popupSurface}
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onInteractOutside={(event) => {
            const target = event.target as Node | null;
            if (target && document.getElementById(id ?? "")?.contains(target)) {
              event.preventDefault();
            }
          }}
        >
          <ul id={listId} role="listbox" aria-labelledby={labelledBy} className="m-0 list-none p-0">
            {matches.length === 0 ? (
              <li role="presentation" className={cn(itemBase, "text-text-muted")}>
                {emptyText}
              </li>
            ) : (
              matches.map((option, index) => (
                // Keyboard selection lives on the input (WAI-ARIA combobox: focus never leaves
                // it, options are reached with aria-activedescendant); the click is for pointers.
                // eslint-disable-next-line jsx-a11y/click-events-have-key-events
                <li
                  key={option.value}
                  id={optionId(index)}
                  role="option"
                  aria-selected={option.value === value}
                  data-highlighted={index === active ? "" : undefined}
                  className={itemBase}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(option)}
                  onMouseMove={() => setActive(index)}
                >
                  {option.label}
                </li>
              ))
            )}
          </ul>
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}
