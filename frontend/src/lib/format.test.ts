import { describe, expect, it } from "vitest";
import { safeHttpUrl } from "./format";

describe("safeHttpUrl", () => {
  it("keeps http(s) links", () => {
    expect(safeHttpUrl("https://boards.example/j/1")).toBe("https://boards.example/j/1");
    expect(safeHttpUrl("http://x.io/a?b=1")).toBe("http://x.io/a?b=1");
  });
  it.each(["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.example", "/relative", "ftp://x/y", "", null, undefined])(
    "drops %s",
    (u) => expect(safeHttpUrl(u as string)).toBeNull(),
  );
});
