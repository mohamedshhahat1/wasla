/**
 * One refresh at a time per browser (S-2, SS-01). The backend signs the whole account out when
 * two refreshes spend one refresh token (J6), so every refresh of every tab of the origin runs
 * under the Web Lock "wasla-refresh". A caller that waited re-reads the holder first: if another
 * call or another tab refreshed meanwhile, it takes that token instead of refreshing again.
 *
 * Correctness comes from the lock alone - with S-2 the refresh token is one cookie per browser,
 * so serialised refreshes always present the newest one. Re-using a token another tab obtained
 * depends on its BroadcastChannel message arriving before this tab's lock is granted; when it
 * does not, this tab refreshes once more with the newest cookie, which is safe.
 */
import type { SessionChannel } from "./session-channel";
import type { TokenHolder, TokenState } from "./token-holder";

export const REFRESH_LOCK = "wasla-refresh";

/** What `fn` returns: the API's refresh answer (FE-2 passes the /session/refresh call). */
export interface RefreshResult {
  access_token: string;
  expires_in: number;
  workspace_slug?: string | null;
}

/** The part of navigator.locks the coordinator uses (a test double implements it). */
export interface LockManagerLike {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>;
}

export interface Clock {
  now(): number;
}

export const systemClock: Clock = { now: () => Date.now() };

export interface RefreshCoordinatorOptions {
  holder: TokenHolder;
  locks: LockManagerLike;
  channel?: SessionChannel;
  clock?: Clock;
}

export class RefreshCoordinator {
  readonly #holder: TokenHolder;
  readonly #locks: LockManagerLike;
  readonly #channel: SessionChannel | undefined;
  readonly #clock: Clock;
  #inFlight: Promise<string> | null = null;

  constructor(options: RefreshCoordinatorOptions) {
    this.#holder = options.holder;
    this.#locks = options.locks;
    this.#channel = options.channel;
    this.#clock = options.clock ?? systemClock;
  }

  /**
   * Returns a fresh access token, calling `fn` only if nobody refreshed since this call began.
   * Callers in the same tab share one attempt; tabs queue on the lock.
   */
  refresh(fn: (workspaceSlug: string | null) => Promise<RefreshResult>): Promise<string> {
    if (this.#inFlight) return this.#inFlight;
    const seenVersion = this.#holder.version;
    const attempt = this.#locks
      .request(REFRESH_LOCK, async () => {
        const now = this.#clock.now();
        const fresh = this.#holder.current(now);
        if (this.#holder.version !== seenVersion && fresh) return fresh;
        const workspaceSlug = this.#holder.state?.workspaceSlug ?? null;
        const result = await fn(workspaceSlug);
        if (!result.access_token || !(result.expires_in > 0)) {
          throw new Error("the refresh answer has no usable access token");
        }
        const issuedAt = this.#clock.now();
        const state: TokenState = {
          accessToken: result.access_token,
          issuedAt,
          expiresAt: issuedAt + result.expires_in * 1000,
          workspaceSlug: result.workspace_slug ?? workspaceSlug,
        };
        this.#holder.set(state);
        this.#channel?.announceToken(state);
        return state.accessToken;
      })
      .finally(() => {
        this.#inFlight = null;
      });
    this.#inFlight = attempt;
    return attempt;
  }
}
