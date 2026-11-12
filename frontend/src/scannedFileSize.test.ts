import { describe, expect, it } from "vitest";
import { formatScannedFileSize, scannedFileSize } from "./scannedFileSize";

describe("saved scan size", () => {
  it.each([0, 1, 1024, Number.MAX_SAFE_INTEGER])("keeps the exact safe byte count %s", (size) => {
    expect(scannedFileSize(size)).toBe(size);
    expect(formatScannedFileSize(size)).toBe(`${size.toLocaleString("ru-RU")} Б`);
  });

  it.each([
    undefined,
    null,
    "",
    "0",
    "1",
    false,
    -1,
    0.5,
    NaN,
    Infinity,
    Number.MAX_SAFE_INTEGER + 1,
  ])("does not turn an absent or invalid size %s into zero", (size) => {
    expect(scannedFileSize(size)).toBeUndefined();
    expect(formatScannedFileSize(size)).toBe("Не зафиксирован");
  });
});
