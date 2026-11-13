/**
 * FE-3's journey hooks (JR-07, JR-09, JR-10), alive only in a development build made with
 * WASLA_E2E_ROUTES=1 (next.config.ts inlines PUBLIC_WASLA_E2E_HOOKS="1" there and "0" everywhere
 * else, so in a real build `hooksOn()` is false and every branch below is dead code).
 *
 * Playwright sets `window.__waslaFe3Flags` with an init script before the app loads, and reads
 * `window.__waslaFe3` - nothing is stored anywhere.
 */
import type { QueryClient } from "@tanstack/react-query";

export interface Fe3Flags {
  /** JR-10: milliseconds added to the clock the gates' focus rule reads. */
  clockOffsetMs?: number;
  /** JR-09's control: every tab takes its own stream (the lock is not asked). */
  bypassStreamLock?: boolean;
}

export interface Fe3Hooks {
  /** JR-07: every mutation's variables in the query client's mutation cache. */
  mutationVariables: () => unknown[];
  /** The coordinator's role and state in this tab. */
  stream: () => { role: string; state: string };
}

declare global {
  interface Window {
    __waslaFe3Flags?: Fe3Flags;
    __waslaFe3?: Fe3Hooks;
  }
}

export function hooksOn(): boolean {
  return process.env.PUBLIC_WASLA_E2E_HOOKS === "1" && typeof window !== "undefined";
}

/** The time the gates' focus rule reads: the wall clock, plus the journeys' offset in a hooks build. */
export function appNow(): number {
  const offset = hooksOn() ? (window.__waslaFe3Flags?.clockOffsetMs ?? 0) : 0;
  return Date.now() + offset;
}

export function bypassStreamLock(): boolean {
  return hooksOn() && window.__waslaFe3Flags?.bypassStreamLock === true;
}

export function installHooks(client: QueryClient, stream: Fe3Hooks["stream"]): void {
  if (!hooksOn()) return;
  window.__waslaFe3 = {
    mutationVariables: () =>
      client
        .getMutationCache()
        .getAll()
        .map((mutation) => mutation.state.variables),
    stream,
  };
}
