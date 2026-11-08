import { headers } from "next/headers";

export async function language(): Promise<string | null> {
  const list = await headers();
  return list.get("Accept-Language");
}
