import { expect, test } from "vitest";

// Canary: proves the vitest + jsdom toolchain itself runs.
test("vitest runs", () => {
  expect(1 + 1).toBe(2);
});

test("jsdom environment is active", () => {
  expect(typeof window).toBe("object");
  expect(typeof document.createElement).toBe("function");
});
