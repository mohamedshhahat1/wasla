/** Shared class lists: the primitives' look, entirely in role tokens (FE-RD, ADR-195). */

/** The ring: 2px in the focus blue on a 2px offset, at once, never animated (CM-06). */
export const focusRing =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

/**
 * Text fields: a 3:1 boundary (border-strong), 44px tall, 16px text (no zoom on iOS), the action
 * blue on hover and focus, the status red when invalid (the message is FormField's).
 */
export const controlBase =
  "min-h-field w-full rounded-md border border-border-strong bg-surface px-4 text-md text-text " +
  "shadow-sm transition-colors placeholder:text-text-muted " +
  "hover:border-primary disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-sunken " +
  "disabled:text-text-muted disabled:shadow-none aria-invalid:border-danger " +
  focusRing;

/** Popover, menu and select panels: raised white, a hairline, the elevation, motion from the trigger. */
export const popupSurface =
  "wasla-pop z-(--z-dropdown) min-w-(--radix-popper-anchor-width) rounded-lg border border-border " +
  "bg-surface-raised p-2 text-md text-text shadow-lg";

/** Menu and list items: the light-blue highlight, the action blue for the checked one. */
export const itemBase =
  "flex min-h-target cursor-pointer select-none items-center gap-3 rounded-md px-4 text-md " +
  "outline-none data-highlighted:bg-secondary-subtle data-disabled:cursor-not-allowed " +
  "data-disabled:text-text-muted data-[state=checked]:font-medium data-[state=checked]:text-primary";
