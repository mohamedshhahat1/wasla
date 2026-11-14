/**
 * The inbox's layout per width (LY-01). The breakpoints are the design tokens' (`--breakpoint-md`,
 * `-lg`, `-xl` in packages/ui/src/tokens.css); a unit test holds them equal.
 */

export const BREAKPOINTS = { md: 768, lg: 1024, xl: 1280 } as const;

export type InboxLayout = "steps" | "drawer" | "panes";

export function layoutFor(width: number): InboxLayout {
  if (width >= BREAKPOINTS.lg) return "panes";
  if (width >= BREAKPOINTS.md) return "drawer";
  return "steps";
}

/**
 * From 1024 px the panel is a pane that can be hidden; it starts shown from 1280 px and hidden
 * between 1024 and 1279 px, where a third pane would leave the conversation too narrow
 * (recorded in DESIGN.md). The person's choice is kept in memory for the session only.
 */
export function panelStartsOpen(width: number): boolean {
  return width >= BREAKPOINTS.xl;
}

/** Which step a phone shows for an address (LY-01: the URL tracks the step). */
export function phoneStep(
  conversationId: string | null,
  panel: boolean,
): "list" | "conversation" | "panel" {
  if (!conversationId) return "list";
  return panel ? "panel" : "conversation";
}
