import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient, unwrap } from "../src";
import type { paths } from "../src";
import { errorHandler, mswPath, typedHandler, type Method, type PathFor } from "./msw";

const here = dirname(fileURLToPath(import.meta.url));
const BASE = "http://api.test";

interface Case {
  name: string;
  method: Method;
  path: string;
  status: number;
  body: { error: Record<string, unknown> };
}
const { cases } = JSON.parse(readFileSync(join(here, "fixtures", "error-bodies.json"), "utf8")) as {
  cases: Case[];
};

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const PATH_PARAMS: Record<string, Record<string, string>> = {
  "/api/v1/knowledge/bases/{knowledge_base_id}/documents/upload": { knowledge_base_id: "kb-1" },
  "/api/v1/conversations/{conversation_id}": { conversation_id: "c-1" },
};

async function call(client: ReturnType<typeof createApiClient>, c: Case): Promise<unknown> {
  const params = PATH_PARAMS[c.path] ? { params: { path: PATH_PARAMS[c.path] } } : {};
  const verb = c.method.toUpperCase() as "GET" | "POST";
  const fn = client[verb] as unknown as (
    path: string,
    init: unknown,
  ) => Promise<{ data?: unknown; error?: unknown; response: Response }>;
  return unwrap(fn(c.path, { ...params, body: c.method === "post" ? {} : undefined }));
}

describe("the normaliser reads FE-0's real error bodies (OA-05)", () => {
  it("has the fourteen bodies of FE-0's table", () => {
    expect(cases).toHaveLength(14);
    expect(new Set(cases.map((c) => c.status))).toEqual(
      new Set([400, 401, 403, 404, 405, 409, 413, 422, 429]),
    );
  });

  for (const c of cases) {
    it(`${c.status} ${String(c.body.error["code"])} (${c.name}) → ApiError by code, never the sentence`, async () => {
      server.use(
        errorHandler(BASE, c.method, c.path as PathFor<"get">, c.status, c.body as never, {
          headers: { "X-Request-ID": "header-request-id" },
        }),
      );
      const client = createApiClient({ baseUrl: BASE });
      const error = await call(client, c).then(
        () => null,
        (e: unknown) => e,
      );
      expect(error).toBeInstanceOf(ApiError);
      const apiError = error as ApiError;
      expect(apiError.status).toBe(c.status);
      expect(apiError.code).toBe(c.body.error["code"]);
      expect(apiError.messageKey).toBe(`errors.${String(c.body.error["code"])}`);
      expect(apiError.message).toBe(c.body.error["code"]);
      expect(apiError.message).not.toBe(c.body.error["message"]);
      expect(apiError.diagnosticMessage).toBe(c.body.error["message"]);
      expect(apiError.requestId).toBe(c.body.error["request_id"] ?? "header-request-id");
    });
  }

  it("maps 422 locations to dotted form fields with the error type", async () => {
    server.use(
      errorHandler(BASE, "post", "/api/v1/auth/login", 422, {
        error: {
          code: "validation_error",
          message: "The request payload is invalid.",
          details: {
            errors: [
              { type: "missing", loc: ["body", "password"], msg: "Field required" },
              { type: "string_too_short", loc: ["body", "profile", "name"], msg: "too short" },
              { type: "int_parsing", loc: ["body", "items", 2, "qty"], msg: "int" },
            ],
          },
          request_id: "r-422",
        },
      }),
    );
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(
      client.POST("/api/v1/auth/login", { body: { email: "a@example.com", password: "" } }),
    ).catch((e: unknown) => e)) as ApiError;
    expect(error.fieldErrors).toEqual({
      password: "missing",
      "profile.name": "string_too_short",
      "items.2.qty": "int_parsing",
    });
  });

  it("keeps Retry-After on a 429", async () => {
    server.use(
      errorHandler(
        BASE,
        "post",
        "/api/v1/auth/login",
        429,
        { error: { code: "rate_limited", message: "Try again in 50 seconds." } },
        { headers: { "Retry-After": "50" } },
      ),
    );
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(
      client.POST("/api/v1/auth/login", { body: { email: "a@example.com", password: "x" } }),
    ).catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("rate_limited");
    expect(error.retryAfterSeconds).toBe(50);
  });
});

describe("bodies that are not the envelope, the network and aborts", () => {
  it("an HTML page from a proxy becomes unexpected_response with the header request id", async () => {
    server.use(
      http.get(mswPath(BASE, "/api/v1/auth/me"), () =>
        HttpResponse.html("<html>502 Bad Gateway</html>", {
          status: 502,
          headers: { "X-Request-ID": "edge-1" },
        }),
      ),
    );
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(client.GET("/api/v1/auth/me")).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error.code).toBe("unexpected_response");
    expect(error.status).toBe(502);
    expect(error.requestId).toBe("edge-1");
    expect(error.messageKey).toBe("errors.unexpected_response");
  });

  it("a JSON body of another shape becomes unexpected_response", async () => {
    server.use(
      http.get(mswPath(BASE, "/api/v1/auth/me"), () =>
        HttpResponse.json({ detail: "Not Found" }, { status: 404 }),
      ),
    );
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(client.GET("/api/v1/auth/me")).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error.code).toBe("unexpected_response");
    expect(error.diagnosticMessage).toBeNull();
  });

  it("a code that is not a code becomes unexpected_response", async () => {
    server.use(
      http.get(mswPath(BASE, "/api/v1/auth/me"), () =>
        HttpResponse.json({ error: { code: "<script>", message: "x" } }, { status: 400 }),
      ),
    );
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(client.GET("/api/v1/auth/me")).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error.code).toBe("unexpected_response");
  });

  it("a network failure becomes network_error", async () => {
    server.use(http.get(mswPath(BASE, "/api/v1/auth/me"), () => HttpResponse.error()));
    const client = createApiClient({ baseUrl: BASE });
    const error = (await unwrap(client.GET("/api/v1/auth/me")).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("network_error");
    expect(error.status).toBe(0);
  });

  it("an abort is re-thrown as an AbortError, not an ApiError", async () => {
    server.use(
      http.get(mswPath(BASE, "/api/v1/auth/me"), async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json({});
      }),
    );
    const client = createApiClient({ baseUrl: BASE });
    const controller = new AbortController();
    const pending = unwrap(client.GET("/api/v1/auth/me", { signal: controller.signal }));
    controller.abort();
    const error = await pending.catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(ApiError);
    expect((error as Error).name).toBe("AbortError");
  });
});

describe("the auth middleware", () => {
  it("adds the bearer when there is a token and none when there is not", async () => {
    const seen: Array<string | null> = [];
    server.use(
      typedHandler(BASE, "get", "/api/v1/timezones", 200, [] as never, {
        onRequest: (request) => seen.push(request.headers.get("Authorization")),
      }),
    );
    let token: string | null = "access-1";
    const client = createApiClient({ baseUrl: BASE, getAccessToken: () => token });
    await unwrap(client.GET("/api/v1/timezones"));
    token = null;
    await unwrap(client.GET("/api/v1/timezones"));
    expect(seen).toEqual(["Bearer access-1", null]);
  });

  it("calls onUnauthenticated exactly once for a 401 and never otherwise", async () => {
    const onUnauthenticated = vi.fn();
    server.use(
      errorHandler(BASE, "get", "/api/v1/auth/me", 401, {
        error: { code: "unauthenticated", message: "The credentials are not valid." },
      }),
      errorHandler(BASE, "get", "/api/v1/conversations", 403, {
        error: { code: "permission_denied", message: "no" },
      }),
    );
    const client = createApiClient({ baseUrl: BASE, onUnauthenticated });
    await unwrap(client.GET("/api/v1/auth/me")).catch(() => undefined);
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
    await unwrap(client.GET("/api/v1/conversations")).catch(() => undefined);
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("returns the data of a 2xx answer", async () => {
    server.use(typedHandler(BASE, "get", "/api/v1/timezones", 200, [] as never));
    const client = createApiClient({ baseUrl: BASE });
    await expect(unwrap(client.GET("/api/v1/timezones"))).resolves.toEqual([]);
  });
});

describe("the multipart override (AC-01)", () => {
  it("types the upload's file as Blob", () => {
    type UploadBody = NonNullable<
      paths["/api/v1/knowledge/bases/{knowledge_base_id}/documents/upload"]["post"]["requestBody"]
    >["content"]["multipart/form-data"];
    const file: UploadBody["file"] = new Blob(["x"]);
    // @ts-expect-error a string is not a file any more
    const notFile: UploadBody["file"] = "text";
    expect(file).toBeInstanceOf(Blob);
    expect(typeof notFile).toBe("string");
  });
});
