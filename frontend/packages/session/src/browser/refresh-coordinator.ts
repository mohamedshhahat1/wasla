/**
 * One refresh at a time per browser (S-2, SS-01). The backend signs the whole account out when
 * two refreshes spend one refresh token (J6), so every refresh of every tab of the origin runs
 * under the Web Lock "wasla-refresh".
 *
 * A caller that waited must not refresh again when another tab already did. The new token
 * reaches it on BroadcastChannel, but browsers do not order that message against the lock grant
 * (Chromium was seen granting first). So a tab that refreshed also HOLDS a marker lock
 * "wasla-token@<issuedAt>@<tab>" until its next refresh; a waiter that gets the refresh lock and finds
 * a marker newer than its own call knows a token is on its way and waits for it (bounded, in case
 * that tab closed) instead of spending the cookie again. Correctness never depends on the
 * hand-off: with S-2 the refresh token is one cookie per browser, so serialised refreshes always
 * present the newest one.
 */
import type { SessionChannel } from "./session-channel";
import type { TokenHolder, TokenState, WorkspaceRef } from "./token-holder";

export const REFRESH_LOCK = "wasla-refresh";
export const TOKEN_MARKER_PREFIX = "wasla-token@";

/** How long a waiter waits for a token announced by a marker before refreshing itself. */
export const HANDOFF_TIMEOUT_MS = 3000;

/** What `fn` returns: the API's refresh answer (FE-2 passes the /session/refresh call). */
export interface RefreshResult {
  access_token: string;
  expires_in: number;
  workspace_slug?: string | null;
  /** The API's active_workspace, when the answer carries one (SB-03). */
  active_workspace?: WorkspaceRef | null;
}

/** The part of navigator.locks the coordinator uses (a test double implements it). */
export interface LockManagerLike {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>;
  query?(): Promise<{ held?: Array<{ name?: string }> }>;
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
  /** Waits for `ms` (injected so tests never wait on the wall clock). */
  delay?: (ms: number) => Promise<void>;
}

const realDelay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class RefreshCoordinator {
  readonly #holder: TokenHolder;
  readonly #locks: LockManagerLike;
  readonly #channel: SessionChannel | undefined;
  readonly #clock: Clock;
  readonly #delay: (ms: number) => Promise<void>;
  #inFlight: Promise<string> | null = null;
  #releaseMarker: (() => void) | null = null;
  /** Unique per coordinator (tab), so two tabs refreshing in the same millisecond never clash. */
  readonly #tab = Math.random().toString(36).slice(2, 10);

  constructor(options: RefreshCoordinatorOptions) {
    this.#holder = options.holder;
    this.#locks = options.locks;
    this.#channel = options.channel;
    this.#clock = options.clock ?? systemClock;
    this.#delay = options.delay ?? realDelay;
  }

  /**
   * Returns a fresh access token, calling `fn` only if nobody refreshed since this call began.
   * Callers in the same tab share one attempt; tabs queue on the lock.
   */
  refresh(fn: (workspaceSlug: string | null) => Promise<RefreshResult>): Promise<string> {
    if (this.#inFlight) return this.#inFlight;
    const seenVersion = this.#holder.version;
    const startedAt = this.#clock.now();
    const attempt = this.#locks
      .request(REFRESH_LOCK, async () => {
        const handedOff = await this.#tokenSince(seenVersion, startedAt);
        if (handedOff) return handedOff;
        return this.#refreshNow(fn);
      })
      .finally(() => {
        this.#inFlight = null;
      });
    this.#inFlight = attempt;
    return attempt;
  }

  /** A token another call or tab obtained after `startedAt`, if one exists or is on its way. */
  async #tokenSince(seenVersion: number, startedAt: number): Promise<string | null> {
    const fresh = () =>
      this.#holder.version !== seenVersion ? this.#holder.current(this.#clock.now()) : null;
    const now = fresh();
    if (now) return now;
    if (!(await this.#newerMarkerExists(startedAt))) return null;
    const arrived = new Promise<string | null>((resolve) => {
      const stop = this.#holder.subscribe(() => {
        const token = fresh();
        if (token) {
          stop();
          resolve(token);
        }
      });
      void this.#delay(HANDOFF_TIMEOUT_MS).then(() => {
        stop();
        resolve(fresh());
      });
    });
    return fresh() ?? (await arrived);
  }

  async #newerMarkerExists(startedAt: number): Promise<boolean> {
    if (!this.#locks.query) return false;
    const { held = [] } = await this.#locks.query();
    return held.some((lock) => {
      const name = lock.name ?? "";
      if (!name.startsWith(TOKEN_MARKER_PREFIX)) return false;
      const issuedAt = Number(name.slice(TOKEN_MARKER_PREFIX.length).split("@")[0]);
      return issuedAt >= startedAt;
    });
  }

  async #refreshNow(fn: (workspaceSlug: string | null) => Promise<RefreshResult>): Promise<string> {
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
      workspaceSlug: result.active_workspace?.slug ?? result.workspace_slug ?? workspaceSlug,
      workspace: result.active_workspace ?? this.#holder.state?.workspace ?? null,
    };
    this.#holder.set(state);
    await this.#holdMarker(issuedAt);
    this.#channel?.announceToken(state);
    return state.accessToken;
  }

  /**
   * Holds "wasla-token@<issuedAt>@<tab>" until this tab's next refresh (or until it closes), and
   * returns only once the lock is granted - before the refresh lock is released.
   */
  async #holdMarker(issuedAt: number): Promise<void> {
    if (!this.#locks.query) return;
    this.#releaseMarker?.();
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    this.#releaseMarker = release;
    await new Promise<void>((granted) => {
      void this.#locks.request(`${TOKEN_MARKER_PREFIX}${issuedAt}@${this.#tab}`, () => {
        granted();
        return held;
      });
    });
  }
}
