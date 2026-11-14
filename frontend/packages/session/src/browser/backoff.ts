/**
 * SB-02: a refresh that fails for the network (not a refusal) is retried by the tab holding the
 * lock, with backoff 1, 2, 4 … capped at 30 s, while the access token is still valid. Once it
 * has expired the caller gets `ConnectionLost` - not a sign-out: the cookie may still be good.
 * A refusal (401/403 from /session/refresh) ends at once as `SignedOut` - or, for a suspended
 * workspace, as `WorkspaceSuspended` (FEF-24, `refreshOutcome`).
 */
import type { Clock } from "./refresh-coordinator";

export const BACKOFF_START_MS = 1000;
export const BACKOFF_CAP_MS = 30_000;

/** The wait before attempt `n` (n = 1 is the first retry): 1, 2, 4 … 30 s. */
export function backoffDelay(attempt: number): number {
  return Math.min(BACKOFF_CAP_MS, BACKOFF_START_MS * 2 ** Math.max(0, attempt - 1));
}

/** The refresh was refused: the session is over. */
export class SignedOut extends Error {
  readonly code: string;
  constructor(code = "unauthenticated") {
    super(code);
    this.name = "SignedOut";
    this.code = code;
  }
}

/**
 * The refresh was refused because the workspace is suspended (FEF-24): the person stays signed
 * in - the cookie is kept and the token was not spent - and the page shows the suspended state.
 */
export class WorkspaceSuspended extends Error {
  readonly code = "workspace_suspended";
  constructor() {
    super("workspace_suspended");
    this.name = "WorkspaceSuspended";
  }
}

/** The network kept failing until the access token expired. */
export class ConnectionLost extends Error {
  constructor() {
    super("connection_lost");
    this.name = "ConnectionLost";
  }
}

/** A failure worth retrying: no answer at all, or the tier / API unavailable for a moment. */
export class TransientFailure extends Error {
  constructor(readonly status: number) {
    super("transient");
    this.name = "TransientFailure";
  }
}

export interface BackoffOptions {
  clock: Clock;
  delay: (ms: number) => Promise<void>;
  /** The current access token's expiry (epoch ms), or null when there is none. */
  expiresAt: () => number | null;
}

/**
 * Runs `attempt` until it succeeds, is refused (SignedOut, re-thrown), or the token in hand
 * expires (ConnectionLost). Any other error counts as transient.
 */
export async function withBackoff<T>(
  attempt: () => Promise<T>,
  options: BackoffOptions,
): Promise<T> {
  for (let tries = 0; ; tries += 1) {
    try {
      return await attempt();
    } catch (error) {
      if (error instanceof SignedOut || error instanceof WorkspaceSuspended) throw error;
      const expiresAt = options.expiresAt();
      const wait = backoffDelay(tries + 1);
      if (expiresAt === null || options.clock.now() + wait >= expiresAt) throw new ConnectionLost();
      await options.delay(wait);
    }
  }
}
