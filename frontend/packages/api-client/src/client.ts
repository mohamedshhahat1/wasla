/**
 * The one API client (AC-03): openapi-fetch keyed by path and method over the generated
 * `paths`, with one middleware that adds the bearer and reports a 401. No retry and no refresh
 * here - packages/session owns refresh and FE-2 wires it through `onUnauthenticated`.
 *
 * Screens call `client.GET("/api/v1/...")` directly (PD-04) and pass the result to `unwrap`,
 * which returns the data or throws an `ApiError`.
 */
import createClient, { type Client, type Middleware } from "openapi-fetch";

import { ApiError, networkError, normaliseError } from "./errors";
import type { paths } from "./schema";

export type WaslaClient = Client<paths>;

export interface ApiClientOptions {
  /** The API origin, e.g. https://api.iwasla.tech (no trailing slash). */
  baseUrl: string;
  /** The in-memory access token, or null when signed out. */
  getAccessToken?: () => string | null;
  /** Called once per 401 response. */
  onUnauthenticated?: (response: Response) => void;
  /** For tests: the fetch to use. */
  fetch?: typeof globalThis.fetch;
}

export function createApiClient(options: ApiClientOptions): WaslaClient {
  const init: Parameters<typeof createClient<paths>>[0] = { baseUrl: options.baseUrl };
  if (options.fetch) init.fetch = options.fetch;
  const client = createClient<paths>(init);
  const middleware: Middleware = {
    onRequest({ request }) {
      const token = options.getAccessToken?.() ?? null;
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) options.onUnauthenticated?.(response);
      return response;
    },
  };
  client.use(middleware);
  return client;
}

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Awaits an openapi-fetch call and returns its data, or throws an ApiError for every non-2xx
 * answer, an unreadable body or a network failure. An abort is re-thrown unchanged.
 */
export async function unwrap<T>(call: Promise<FetchResult<T>>): Promise<T> {
  let result: FetchResult<T>;
  try {
    result = await call;
  } catch (cause) {
    throw networkError(cause);
  }
  if (!result.response.ok) throw normaliseError(result.error, result.response);
  return result.data as T;
}

export { ApiError };
