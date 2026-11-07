/**
 * MSW handlers typed by the generated contract (AC-05, PD-16): a handler for a path, a
 * method or a response body the document does not have fails to typecheck, so a mock cannot
 * drift from the backend's real shapes.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { ErrorEnvelope, ValidationErrorEnvelope } from "../src/errors";
import type { paths } from "../src/schema";

export type Method = "get" | "post" | "put" | "patch" | "delete";

type OperationOf<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;
type HasOperation<P extends keyof paths, M extends Method> = [OperationOf<P, M>] extends [never]
  ? false
  : true;

/** The paths that have an operation for this method. */
export type PathFor<M extends Method> = {
  [P in keyof paths]: HasOperation<P, M> extends true ? P : never;
}[keyof paths];

type ResponsesOf<P extends keyof paths, M extends Method> =
  OperationOf<P, M> extends {
    responses: infer R;
  }
    ? R
    : never;

/** The declared numeric statuses of one operation. */
export type StatusFor<P extends keyof paths, M extends Method> = Extract<
  keyof ResponsesOf<P, M>,
  number
>;

type JsonOf<R> = R extends { content: { "application/json": infer B } } ? B : undefined;

/** The JSON body the contract declares for this status. */
export type BodyFor<P extends keyof paths, M extends Method, S extends StatusFor<P, M>> = JsonOf<
  ResponsesOf<P, M>[S]
>;

/** `/api/v1/a/{id}` → `<base>/api/v1/a/:id` */
export function mswPath(baseUrl: string, path: string): string {
  return baseUrl + path.replace(/\{([^}]+)\}/g, ":$1");
}

export interface TypedHandlerOptions {
  headers?: Record<string, string>;
  onRequest?: (request: Request) => void;
}

/** A handler answering one declared status with a body the contract declares for it. */
export function typedHandler<M extends Method, P extends PathFor<M>, S extends StatusFor<P, M>>(
  baseUrl: string,
  method: M,
  path: P,
  status: S,
  body: BodyFor<P, M, S>,
  options: TypedHandlerOptions = {},
): HttpHandler {
  return http[method](mswPath(baseUrl, path), ({ request }) => {
    options.onRequest?.(request);
    const init = { status, headers: options.headers ?? {} };
    return body === undefined ? new HttpResponse(null, init) : HttpResponse.json(body, init);
  });
}

/** Any error status with the envelope every operation declares as `default` (FEB-02). */
export function errorHandler<M extends Method, P extends PathFor<M>>(
  baseUrl: string,
  method: M,
  path: P,
  status: number,
  body: ErrorEnvelope | ValidationErrorEnvelope,
  options: TypedHandlerOptions = {},
): HttpHandler {
  return http[method](mswPath(baseUrl, path), ({ request }) => {
    options.onRequest?.(request);
    return HttpResponse.json(body, { status, headers: options.headers ?? {} });
  });
}
