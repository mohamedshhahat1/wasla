/**
 * The customer app's browser session (FE-2, SB-01..SB-03, ADR-193), framework-free so it can be
 * unit-tested: FE-1's TokenHolder (memory only), RefreshCoordinator (one refresh per browser
 * under the Web Lock) and SessionChannel (BroadcastChannel), wired to the /session/* routes.
 *
 * The page never sees the refresh token: it lives in an HttpOnly cookie the routes alone read.
 */
import {
  ConnectionLost,
  RefreshCoordinator,
  SessionChannel,
  SignedOut,
  TokenHolder,
  TransientFailure,
  withBackoff,
  WorkspaceSuspended,
  refreshOutcome,
  type ChannelLike,
  type Clock,
  type LockManagerLike,
  type RefreshResult,
  type TokenState,
  type WorkspaceRef,
} from "@wasla/session";

import { ApiError, createApiClient, normaliseError, networkErrorOf, type WaslaClient } from "./api";

/**
 * `workspace-suspended` (FEF-24): the refresh was refused only because the workspace is
 * suspended - the person is signed in (the cookie is kept), holds no usable token for it, and
 * the page shows the suspended state with "Check again" (`restore`) and "Sign out".
 */
export type SessionStatus =
  "loading" | "signed-out" | "signed-in" | "connection-lost" | "workspace-suspended";

/** What a session route answers on success (never a refresh token). */
export interface SessionGrant {
  access_token: string;
  expires_in: number;
  active_workspace: WorkspaceRef | null;
}

export type RegisterOutcome = { kind: "signed-in" } | { kind: "check_inbox" };

export interface WebSessionOptions {
  apiOrigin: string;
  locks: LockManagerLike;
  channel: ChannelLike;
  clock?: Clock;
  delay?: (ms: number) => Promise<void>;
  /** For tests: the fetch the session routes and the API client use. */
  fetch?: typeof globalThis.fetch;
}

type Listener = () => void;

const realDelay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** How long a tab that just opened waits for another tab's token before it refreshes (JT-03). */
export const TOKEN_HANDOFF_WAIT_MS = 300;

export class WebSession {
  readonly holder = new TokenHolder();
  readonly channel: SessionChannel;
  readonly coordinator: RefreshCoordinator;
  readonly api: WaslaClient;
  readonly #clock: Clock;
  readonly #delay: (ms: number) => Promise<void>;
  readonly #fetch: typeof globalThis.fetch;
  readonly #listeners = new Set<Listener>();
  readonly #blobUrls = new Set<string>();
  #status: SessionStatus = "loading";
  /** The holder version this tab itself last set - anything newer came from another tab. */
  #ownVersion = -1;

  constructor(options: WebSessionOptions) {
    this.#clock = options.clock ?? { now: () => Date.now() };
    this.#delay = options.delay ?? realDelay;
    this.#fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.channel = new SessionChannel(this.holder, options.channel);
    this.coordinator = new RefreshCoordinator({
      holder: this.holder,
      locks: options.locks,
      channel: this.channel,
      clock: this.#clock,
      delay: this.#delay,
    });
    this.api = createApiClient({
      baseUrl: options.apiOrigin,
      getAccessToken: () => this.holder.current(this.#clock.now()),
      fetch: (input: RequestInfo | URL, init?: RequestInit) =>
        this.#authFetch(input instanceof Request ? input : new Request(input, init)),
    });
    this.holder.subscribe((state) => {
      if (state) this.#setStatus("signed-in");
      else if (this.#status !== "loading") this.#signedOutLocally();
    });
  }

  get status(): SessionStatus {
    return this.#status;
  }

  /** The active workspace of the token in hand. */
  get workspace(): WorkspaceRef | null {
    return this.holder.state?.workspace ?? null;
  }

  /** True when the latest token came from another tab (a switch there, WK-02). */
  get lastChangeFromAnotherTab(): boolean {
    return this.holder.version !== this.#ownVersion;
  }

  subscribe(listener: Listener): () => void {
    this.#listeners.add(listener);
    const stop = this.holder.subscribe(() => listener());
    return () => {
      this.#listeners.delete(listener);
      stop();
    };
  }

  /**
   * On page load: first ask the open tabs for their valid token (a tab joining never refreshes
   * while another holds one, JT-03); none within TOKEN_HANDOFF_WAIT_MS → one refresh under the
   * lock; no cookie (401) → signed out, without an error.
   */
  async restore(): Promise<void> {
    let stop: () => void = () => undefined;
    const handedOver = new Promise<void>((resolve) => {
      stop = this.holder.subscribe((state) => {
        if (state) resolve();
      });
    });
    this.channel.askForToken();
    await Promise.race([handedOver, this.#delay(TOKEN_HANDOFF_WAIT_MS)]);
    stop();
    if (this.holder.current(this.#clock.now())) {
      // Adopted, not a change made elsewhere: no "switched in another tab" notice for it.
      this.#ownVersion = this.holder.version;
      return;
    }
    try {
      await this.refresh();
    } catch (error) {
      if (error instanceof ConnectionLost) this.#setStatus("connection-lost");
      else if (error instanceof WorkspaceSuspended) this.#setStatus("workspace-suspended");
      else this.#setStatus("signed-out");
    }
  }

  /** A fresh access token: one refresh per browser (the coordinator), backoff on the network. */
  refresh(coordinator: RefreshCoordinator = this.coordinator): Promise<string> {
    return coordinator
      .refresh((workspaceSlug) =>
        withBackoff(() => this.#refreshOnce(workspaceSlug), {
          clock: this.#clock,
          delay: this.#delay,
          expiresAt: () => this.holder.state?.expiresAt ?? null,
        }),
      )
      .then((token) => {
        this.#ownVersion = this.holder.version;
        return token;
      })
      .catch((error: unknown) => {
        // A tab that was signed in tells every tab; a tab restoring without a cookie only itself.
        if (error instanceof SignedOut) {
          if (this.holder.state) this.channel.announceSignedOut();
          else this.#setStatus("signed-out");
        }
        if (error instanceof ConnectionLost) this.#setStatus("connection-lost");
        if (error instanceof WorkspaceSuspended) this.#setStatus("workspace-suspended");
        throw error;
      });
  }

  /** A token that is still valid, refreshing first when there is none. */
  async accessToken(): Promise<string> {
    return this.holder.current(this.#clock.now()) ?? (await this.refresh());
  }

  async login(email: string, password: string, workspaceSlug?: string | null): Promise<void> {
    const grant = await this.#post<SessionGrant>("login", {
      email,
      password,
      ...(workspaceSlug ? { workspace_slug: workspaceSlug } : {}),
    });
    this.#adopt(grant);
  }

  async registerLogin(form: Record<string, string | null>): Promise<RegisterOutcome> {
    const answer = await this.#post<SessionGrant | { status: "check_inbox" }>(
      "register-login",
      form,
    );
    if ("status" in answer) return { kind: "check_inbox" };
    this.#adopt(answer);
    return { kind: "signed-in" };
  }

  /** WK-01: the new token for `slug`, broadcast to every tab; the old token is forgotten. */
  async switchWorkspace(slug: string): Promise<void> {
    const bearer = await this.accessToken();
    const grant = await this.#post<SessionGrant>("switch", { workspace_slug: slug }, bearer);
    this.#adopt(grant);
  }

  async googleAuthorizeUrl(): Promise<string> {
    const answer = await this.#post<{ authorization_url: string }>("google/authorize", {});
    return answer.authorization_url;
  }

  async googleCallback(code: string, state: string): Promise<void> {
    this.#adopt(await this.#post<SessionGrant>("google/callback", { code, state }));
  }

  async signOut(): Promise<void> {
    try {
      await this.#post("logout", {});
    } finally {
      this.channel.announceSignedOut();
    }
  }

  async signOutEverywhere(): Promise<void> {
    const bearer = await this.accessToken();
    await this.#post("logout-all", {}, bearer);
    this.channel.announceSignedOut();
  }

  /** Blob URLs this tab made, revoked on sign-out (SB-01). */
  trackBlobUrl(url: string): void {
    this.#blobUrls.add(url);
  }

  close(): void {
    this.channel.close();
  }

  #adopt(grant: SessionGrant): void {
    if (!grant.access_token || !(grant.expires_in > 0)) {
      throw new ApiError({ status: 0, code: "unexpected_response" });
    }
    const issuedAt = this.#clock.now();
    const state: TokenState = {
      accessToken: grant.access_token,
      issuedAt,
      expiresAt: issuedAt + grant.expires_in * 1000,
      workspaceSlug: grant.active_workspace?.slug ?? null,
      workspace: grant.active_workspace,
    };
    this.holder.set(state);
    this.#ownVersion = this.holder.version;
    this.channel.announceToken(state);
  }

  async #refreshOnce(workspaceSlug: string | null): Promise<RefreshResult> {
    let response: Response;
    try {
      response = await this.#rawPost(
        "refresh",
        workspaceSlug ? { workspace_slug: workspaceSlug } : {},
      );
    } catch {
      throw new TransientFailure(0);
    }
    const body: unknown = await response.json().catch(() => null);
    if (response.ok) return body as RefreshResult;
    const refusal = normaliseError(body, response);
    switch (refreshOutcome(response.status, refusal.code)) {
      case "workspace-suspended":
        throw new WorkspaceSuspended();
      case "signed-out":
        throw new SignedOut(refusal.code);
      case "transient":
        throw new TransientFailure(response.status);
      default:
        throw refusal;
    }
  }

  #rawPost(verb: string, body: Record<string, unknown>, bearer?: string): Promise<Response> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Wasla-Session": "1",
    };
    if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
    return this.#fetch(`/session/${verb}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  }

  async #post<T>(verb: string, body: Record<string, unknown>, bearer?: string): Promise<T> {
    let response: Response;
    try {
      response = await this.#rawPost(verb, body, bearer);
    } catch (cause) {
      throw networkErrorOf(cause);
    }
    const parsed: unknown = await response.json().catch(() => null);
    if (!response.ok) throw normaliseError(parsed, response);
    return parsed as T;
  }

  /** SB-01: a 401 refreshes once under the lock and retries the request once; a second 401 signs out. */
  async #authFetch(request: Request): Promise<Response> {
    const retry = request.clone();
    const response = await this.#fetch(request);
    if (response.status !== 401 || !request.headers.has("Authorization")) return response;
    let token: string;
    try {
      token = await this.refresh();
    } catch {
      return response;
    }
    retry.headers.set("Authorization", `Bearer ${token}`);
    const second = await this.#fetch(retry);
    if (second.status === 401) this.channel.announceSignedOut();
    return second;
  }

  #signedOutLocally(): void {
    for (const url of this.#blobUrls) URL.revokeObjectURL(url);
    this.#blobUrls.clear();
    this.#setStatus("signed-out");
  }

  #setStatus(status: SessionStatus): void {
    if (this.#status === status) return;
    this.#status = status;
    for (const listener of this.#listeners) listener();
  }
}
