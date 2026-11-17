/**
 * The knowledge-bases list answers its next page in `X-Next-Cursor` (FEB-39: exposed through CORS
 * so this origin can read it). The cursor is the API's, passed back as given - never built here.
 */
import { unwrap } from "@wasla/api-client";

export const NEXT_CURSOR_HEADER = "X-Next-Cursor";

export interface CursorList<T> {
  items: T[];
  nextCursor: string | null;
}

type Call<T> = Parameters<typeof unwrap<T>>[0];

/** The list a call answers, and its next cursor from the response header (null at the end). */
export async function withHeaderCursor<T>(call: Call<T[]>): Promise<CursorList<T>> {
  let headers: Headers | null = null;
  const items = await unwrap(
    call.then((result) => {
      headers = result.response.headers;
      return result;
    }),
  );
  const cursor = (headers as Headers | null)?.get(NEXT_CURSOR_HEADER) ?? null;
  return { items, nextCursor: cursor ? cursor : null };
}
