import { describe, expect, it } from "vitest";

import { normalizeEmailBody } from "./emailText";

describe("normalizeEmailBody", () => {
  it("turns escaped newline markers into paragraph breaks", () => {
    expect(normalizeEmailBody("First paragraph.\\n\\nSecond paragraph.")).toBe(
      "First paragraph.\n\nSecond paragraph.",
    );
  });

  it("normalizes Windows newlines without changing ordinary text", () => {
    expect(normalizeEmailBody("First line.\r\nSecond line.")).toBe(
      "First line.\nSecond line.",
    );
    expect(normalizeEmailBody("No formatting changes needed.")).toBe(
      "No formatting changes needed.",
    );
  });
});
