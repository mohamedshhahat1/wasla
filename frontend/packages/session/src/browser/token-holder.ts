/**
 * The access token in memory only (S-2, SS-01): never storage, never a cookie. `version` moves
 * on every new token; `generation` moves on sign-out or a workspace switch and keys the query
 * cache (PD-06), so nothing cached under one session is read under the next.
 */

export interface TokenState {
  accessToken: string;
  /** Epoch milliseconds at which the token expires. */
  expiresAt: number;
  /** Epoch milliseconds at which it was issued (for the 80 % schedule). */
  issuedAt: number;
  /** The workspace the token is scoped to, sent with every refresh (plan 8.1). */
  workspaceSlug: string | null;
}

export type HolderListener = (state: TokenState | null, holder: TokenHolder) => void;

export class TokenHolder {
  #state: TokenState | null = null;
  #version = 0;
  #generation = 0;
  readonly #listeners = new Set<HolderListener>();

  /** Moves on every token set, from this tab or another. */
  get version(): number {
    return this.#version;
  }

  /** Moves on sign-out and on workspace switch; part of every query key. */
  get generation(): number {
    return this.#generation;
  }

  get state(): TokenState | null {
    return this.#state;
  }

  /** The token if it has not expired at `now`; null otherwise. */
  current(now: number): string | null {
    const state = this.#state;
    return state && state.expiresAt > now ? state.accessToken : null;
  }

  set(state: TokenState): void {
    if (!state.accessToken) throw new Error("an empty access token");
    const switched = this.#state !== null && this.#state.workspaceSlug !== state.workspaceSlug;
    this.#state = { ...state };
    this.#version += 1;
    if (switched) this.#generation += 1;
    this.#emit();
  }

  /** Signed out (here or in another tab): forget the token and start a new generation. */
  clear(): void {
    this.#state = null;
    this.#version += 1;
    this.#generation += 1;
    this.#emit();
  }

  subscribe(listener: HolderListener): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #emit(): void {
    for (const listener of this.#listeners) listener(this.#state, this);
  }
}
