/**
 * ST-05: a switch closes this workspace's stream BEFORE the new workspace's token is adopted,
 * and re-joins it when the switch fails. One function, so the order is tested once.
 */
export interface SwitchSteps {
  /** Closes the current workspace's stream (and releases its lock). */
  leave: () => void;
  /** FE-2's switch: the new token, broadcast to every tab. */
  switchWorkspace: (slug: string) => Promise<void>;
  /** After a refused switch: the current workspace's stream again. */
  rejoin: () => void;
}

export async function switchTo(slug: string, steps: SwitchSteps): Promise<void> {
  steps.leave();
  try {
    await steps.switchWorkspace(slug);
  } catch (error) {
    steps.rejoin();
    throw error;
  }
}
