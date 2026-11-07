/** Shared class lists: the primitives' look, entirely in tokens. */

export const focusRing =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

export const controlBase =
  "min-h-target w-full rounded-md border border-border-strong bg-surface px-4 text-md text-text " +
  "placeholder:text-text-muted disabled:cursor-not-allowed disabled:bg-surface-sunken " +
  "disabled:text-text-muted aria-invalid:border-danger " +
  focusRing;

export const popupSurface =
  "z-(--z-dropdown) min-w-(--radix-popper-anchor-width) rounded-md border border-border " +
  "bg-surface-raised p-2 text-md text-text shadow-md";

export const itemBase =
  "flex min-h-target cursor-default select-none items-center gap-3 rounded-sm px-4 text-md " +
  "outline-none data-highlighted:bg-surface-sunken data-disabled:text-text-muted " +
  "data-[state=checked]:font-medium";
