import { cookies } from "next/headers";

export async function readSomething(): Promise<string | undefined> {
  const jar = await cookies();
  return jar.get("anything")?.value;
}

export function write(headers: Headers): void {
  headers.append("Set-Cookie", "x=1; Path=/");
}
