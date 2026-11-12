import { describe, expect, it } from "vitest";

/** VQ-03: the colour-role measurement, on synthetic images whose shares are known. */
interface Shares {
  dominant: number;
  secondary: number;
  accent: number;
  status: number;
  textOther: number;
}
interface Module {
  loadPalette(): unknown[];
  classifier(palette: unknown[]): (r: number, g: number, b: number) => string;
  shares(image: { width: number; height: number; data: Uint8Array }, classify: unknown): Shares;
  screenProblems(name: string, shares: Shares): string[];
  averageProblems(rows: Array<{ name: string; shares: Shares }>): {
    average: Shares;
    problems: string[];
  };
}

const script = new URL("../../../scripts/measure-colour-roles.mjs", import.meta.url).href;

/** A 100-pixel image painted in horizontal stripes: [hex, pixel count] in order. */
function image(stripes: Array<[string, number]>) {
  const data = new Uint8Array(100 * 4);
  let at = 0;
  for (const [hex, count] of stripes) {
    const n = parseInt(hex.slice(1), 16);
    for (let i = 0; i < count; i++, at++) {
      data.set([(n >> 16) & 255, (n >> 8) & 255, n & 255, 255], at * 4);
    }
  }
  expect(at).toBe(100);
  return { width: 10, height: 10, data };
}

describe("measure-colour-roles (VQ-03)", async () => {
  const mod = (await import(script)) as Module;
  const classify = mod.classifier(mod.loadPalette());

  it("classifies the token colours by role", () => {
    expect(classify(0xff, 0xff, 0xff)).toBe("dominant"); // surface white
    expect(classify(0xf4, 0xf8, 0xfe)).toBe("dominant"); // canvas blue-50
    expect(classify(0x12, 0x2f, 0x5e)).toBe("secondary"); // brand blue-900
    expect(classify(0xe4, 0xee, 0xfc)).toBe("secondary"); // secondary-subtle blue-100
    expect(classify(0x1c, 0x5f, 0xc2)).toBe("accent"); // primary blue-600
    expect(classify(0xb4, 0x23, 0x18)).toBe("status"); // danger
    expect(classify(0x0a, 0x1a, 0x35)).toBe("text"); // text blue-950
    expect(classify(0x80, 0x00, 0x80)).toBe("other"); // a violet no token has
  });

  it("reads the theme's value, not a scoped override defined later", async () => {
    const { tokenColours } = (await import(script)) as {
      tokenColours(css: string): Record<string, string>;
    };
    const css = `@theme { --color-primary: #1c5fc2; } :root[data-app="console"] { --color-primary: #153e7d; }`;
    expect(tokenColours(css)["color-primary"]).toBe("#1c5fc2");
  });

  it("measures a 60 / 30 / 10 image as 60 / 30 / 10", () => {
    const s = mod.shares(
      image([
        ["#ffffff", 60],
        ["#122f5e", 30],
        ["#1c5fc2", 10],
      ]),
      classify,
    );
    expect([s.dominant, s.secondary, s.accent, s.status, s.textOther]).toEqual([60, 30, 10, 0, 0]);
    expect(mod.screenProblems("good-1280.png", s)).toEqual([]);
  });

  it("keeps text and unknown colours out of the role shares", () => {
    const s = mod.shares(
      image([
        ["#ffffff", 50],
        ["#122f5e", 20],
        ["#0a1a35", 20],
        ["#800080", 10],
      ]),
      classify,
    );
    expect(s.textOther).toBe(30);
    expect(s.dominant).toBeCloseTo(71.4, 1);
    expect(s.secondary).toBeCloseTo(28.6, 1);
  });

  it("fails an all-accent screen and a screen outside the hard limits", () => {
    const allAccent = mod.shares(image([["#1c5fc2", 100]]), classify);
    expect(mod.screenProblems("loud-1280.png", allAccent)).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^accent/),
        expect.stringMatching(/^dominant/),
      ]),
    );
    const red = mod.shares(
      image([
        ["#ffffff", 90],
        ["#b42318", 10],
      ]),
      classify,
    );
    expect(mod.screenProblems("calm-1280.png", red)).toEqual([expect.stringMatching(/^status/)]);
    expect(mod.screenProblems("form-error-1280.png", red)).toEqual([]);
  });

  it("checks the averages against the bands", () => {
    const good = { dominant: 60, secondary: 30, accent: 10, status: 0, textOther: 10 };
    const flat = { dominant: 95, secondary: 3, accent: 2, status: 0, textOther: 10 };
    expect(mod.averageProblems([{ name: "a.png", shares: good }]).problems).toEqual([]);
    expect(mod.averageProblems([{ name: "b.png", shares: flat }]).problems).toHaveLength(3);
    const loud = { dominant: 50, secondary: 30, accent: 20, status: 0, textOther: 10 };
    expect(mod.averageProblems([{ name: "c.png", shares: loud }]).problems).toEqual(
      expect.arrayContaining([expect.stringMatching(/^average accent/)]),
    );
  });
});
