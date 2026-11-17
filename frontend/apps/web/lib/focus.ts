/**
 * Focus that was lost - the control that held it was removed (a dialog closed into a new page, a
 * status button replaced by the next one) - is given to a known element, so the next Tab starts
 * from there. Chromium wraps a lost focus to the top of the page; Firefox and WebKit move it to the
 * browser's own controls, where a keyboard user would never get back (KY-02).
 */
export function isFocusLost(doc: Document = document): boolean {
  const active = doc.activeElement;
  return active === null || active === doc.body || !active.isConnected;
}

/** Focuses `target` only when focus was lost; a focus the person placed is never moved. */
export function focusIfLost(
  target: HTMLElement | null | undefined,
  doc: Document = document,
): void {
  if (target && isFocusLost(doc)) target.focus();
}
