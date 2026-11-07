const memory = new Map<string, string>();
export function keep(value: string) {
  memory.set("x", value);
  return { localStorage: "a key name, not the global" };
}
