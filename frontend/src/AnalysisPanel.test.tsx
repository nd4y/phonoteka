import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AnalysisPanel } from "./AnalysisPanel";
import type { Track } from "./types";
const track: Track = {
  id: "test",
  relative_path: "demo.mp3",
  sha256: "a".repeat(64),
  revision: "a".repeat(64),
  format: "mp3",
  tags: {},
  has_art: false,
  status: "ready",
  can_write: true,
};
const render = (value: Track, asrEnabled = false) =>
  renderToStaticMarkup(
    <AnalysisPanel
      track={value}
      enabled
      recognitionEnabled
      asrEnabled={asrEnabled}
      busy={false}
      changed={false}
      onJob={async () => {}}
      onRefresh={() => {}}
    />,
  );
describe("source search presentation", () => {
  it("explains an exhausted recognition allowance in the source details", () => {
    const html = render({
      ...track,
      analysis: {
        input_sha256: track.sha256,
        query_source: "embedded",
        state: "candidates",
        candidates: [],
        sources: [{ provider: "shazam", state: "recognition_attempt_budget_exhausted" }],
      },
    });
    expect(html).toContain("Лимит распознавания по аудио исчерпан");
    expect(html).not.toContain("recognition_attempt_budget_exhausted");
  });
  it("offers an explicit language for both audio checking and alignment", () => {
    const html = render({ ...track, lrc: "[00:01.00]Слова песни" }, true);
    expect(html).toContain("Язык записи<select>");
    expect(html).toContain('<option value="" selected="">Определить автоматически</option>');
    expect(html).toContain('<option value="ru">Русский</option>');
    expect(html).toContain('<option value="en">Английский</option>');
    expect(html).toContain("Проверить содержимое аудио");
    expect(html).toContain("Свести текст с аудио");
    expect(render(track)).toContain('Язык записи<select disabled="">');
  });
  it("focuses attention on unresolved timing without accepting the remaining lines", () => {
    const html = render({
      ...track,
      analysis: {
        input_sha256: track.sha256,
        query_source: "embedded",
        state: "candidates",
        candidates: [],
        sources: [],
        alignment: {
          state: "partially_aligned",
          coverage: 0.5,
          complete: false,
          lines: [
            { text: "Unresolved line", start_ms: null, original_ms: null, state: "unaligned" },
            { text: "Anchored line", start_ms: 1200, original_ms: null, state: "anchored" },
          ],
        },
      },
    });
    expect(html).toContain("Unresolved line");
    expect(html).not.toContain("Anchored line");
    expect(html).toContain("Остальные совпадения также требуют проверки на слух");
    expect(html).toContain("Показать все строки");
  });
  it("names a selectable album edition separately from the song", () => {
    const html = render({
      ...track,
      analysis: {
        input_sha256: track.sha256,
        query_source: "embedded",
        state: "candidates",
        sources: [],
        candidates: [
          {
            id: "release",
            provider: "musicbrainz",
            kind: "release",
            title: "Album",
            tags: { album: "Album" },
            match: { state: "needs_review", reasons: ["confirm_release"] },
          },
        ],
      },
    });
    expect(html).toContain("Альбом: Album");
    expect(html).toContain("Проверьте релиз");
  });
  it("does not create an automatically open disclosure for the first search, preserving one-step Back", () => {
    const html = render(track);
    expect(html).toContain('<section class="workflow-job-details source-search-actions">');
    expect(html).not.toContain('open=""');
    expect(html).toContain("Если песня неизвестна, начните с распознавания аудио.");
  });
  it("keeps repeat search collapsed while candidates remain available for review", () => {
    const html = render({
      ...track,
      analysis: {
        input_sha256: track.sha256,
        query_source: "audio_fingerprint",
        state: "candidates",
        candidates: [
          {
            id: "one",
            provider: "musicbrainz",
            kind: "metadata",
            tags: { title: "Тест" },
            match: { state: "candidate", reasons: [] },
          },
        ],
        sources: [],
      },
    });
    expect(html).toContain('<details class="workflow-job-details source-search-actions">');
    expect(html).not.toContain('open=""');
    expect(html).toContain("Сравнить изменения");
  });
});
