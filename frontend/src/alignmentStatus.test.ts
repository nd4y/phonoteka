import { describe, expect, it } from "vitest";
import { alignmentStatus, instrumentalTextConflict } from "./alignmentStatus";
import type { Analysis, Track } from "./types";

describe("ASR result explanation", () => {
  const analysis = (words: unknown[], lines: object[]) =>
    ({
      asr: { words },
      alignment: { lines: lines.map((line) => ({ text: "Words", ...line })), coverage: 0 },
    }) as unknown as Analysis;
  it("distinguishes no ASR words from a broken timing algorithm", () => {
    expect(
      alignmentStatus(analysis([], [{ start_ms: null, reason: "independent_anchor_missing" }]))
        ?.code,
    ).toBe("no_recognized_words");
  });
  it("distinguishes recognized words without complete line anchors", () => {
    expect(
      alignmentStatus(
        analysis(
          [{ probability: 0.9 }, { probability: 0.1 }],
          [{ start_ms: null, reason: "independent_anchor_missing" }],
        ),
      )?.code,
    ).toBe("no_independent_anchors");
  });
  it("does not call full timing coverage accepted", () => {
    expect(
      alignmentStatus(
        analysis([{ probability: 0.9 }], [{ start_ms: 1000, reason: "requires_listening" }]),
      )?.code,
    ).toBe("requires_listening");
  });
  it("does not describe missing model confidence as zero reliable words", () => {
    const value = analysis(
      [{ word: "example", start: 0, end: 1 }],
      [{ start_ms: null, reason: "independent_anchor_missing" }],
    );
    value.asr!.confidence_available = false;
    const status = alignmentStatus(value);
    expect(status?.code).toBe("no_independent_anchors");
    expect(status?.detail).toContain("не предоставляет оценку уверенности");
    expect(status?.detail).not.toContain("0 с достаточной уверенностью");
  });
  it("counts lyric content while leaving empty LRC markers unverified", () => {
    expect(
      alignmentStatus(
        analysis(
          [{ probability: 0.9 }],
          [
            { text: "Words", start_ms: 1000 },
            { text: "", start_ms: null },
          ],
        ),
      )?.code,
    ).toBe("requires_listening");
    expect(
      alignmentStatus(analysis([{ probability: 0.9 }], [{ text: " ", start_ms: null }]))?.code,
    ).toBe("empty_reference");
  });
  it("warns about words proposed for an instrumental title without classifying the audio", () => {
    const track = (values: Partial<Track>): Track => ({
      id: "test",
      relative_path: "test.mp3",
      sha256: "test",
      revision: "test",
      format: "mp3",
      tags: {},
      has_art: false,
      status: "scanned",
      can_write: false,
      ...values,
    });
    const proposal = { id: "proposal", input_sha256: "test", tags: {}, lrc: "words" };
    expect(
      instrumentalTextConflict(track({ tags: { title: ["Example (Instrumental)"] }, proposal })),
    ).toBe(true);
    expect(instrumentalTextConflict(track({ tags: { title: "Example" }, proposal }))).toBe(false);
    expect(instrumentalTextConflict(track({ tags: { title: "Example (Instrumental)" } }))).toBe(
      false,
    );
    expect(
      instrumentalTextConflict(
        track({ tags: { title: "Пример (Инструментал)" }, lyrics: "words" }),
      ),
    ).toBe(true);
  });
});
