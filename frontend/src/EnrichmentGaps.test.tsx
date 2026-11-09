import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EnrichmentGaps, GapSummary } from "./EnrichmentGaps";
import type { EnrichmentGap } from "./types";

const gap: EnrichmentGap = {
  field: "cover",
  code: "cover_not_searched",
  message: "Поиск обложки ещё не выполнен.",
  action: "sources",
  next_step: "Проверьте альбом и варианты изданий.",
  analysis_current: true,
  provider: "coverartarchive",
  checked_at: "2026-11-07T10:00:00Z",
};

describe("visible reasons for missing fields", () => {
  it("shows the cause in the catalog without relying on hover", () => {
    const html = renderToStaticMarkup(<GapSummary gaps={[gap, { ...gap, field: "album" }]} />);
    expect(html).toContain("Обложка: Поиск обложки ещё не выполнен.");
    expect(html).toContain("Ещё 1");
  });
  it("shows the reason for the active cover or timed-text filter", () => {
    const gaps: EnrichmentGap[] = [
      { ...gap, field: "album", message: "Нужно выбрать альбом." },
      gap,
      { ...gap, field: "synced_lyrics", message: "Найден текст без таймкодов." },
    ];
    for (const filter of ["missing_art", "music_missing_art"]) {
      const html = renderToStaticMarkup(<GapSummary gaps={gaps} filter={filter} />);
      expect(html).toContain("Обложка: Поиск обложки ещё не выполнен.");
      expect(html).not.toContain("Нужно выбрать альбом.");
    }
    for (const filter of ["missing_lyrics", "music_missing_lyrics"]) {
      const html = renderToStaticMarkup(<GapSummary gaps={gaps} filter={filter} />);
      expect(html).toContain("Текст с таймкодами: Найден текст без таймкодов.");
      expect(html).not.toContain("Нужно выбрать альбом.");
    }
    expect(renderToStaticMarkup(<GapSummary gaps={[gaps[0]]} filter="music_missing_art" />)).toBe(
      "",
    );
  });
  it("shows evidence and a next action while respecting unsaved edits", () => {
    const html = renderToStaticMarkup(<EnrichmentGaps gaps={[gap]} disabled onAction={() => {}} />);
    expect(html).toContain(gap.next_step);
    expect(html).toContain("coverartarchive");
    expect(html).toContain("Открыть источники");
    expect(html).toContain('disabled=""');
  });
  it("does not call absent diagnostic evidence complete", () => {
    expect(renderToStaticMarkup(<GapSummary />)).toBe("");
  });
  it("labels a failed job timestamp without claiming the analysis was refreshed", () => {
    const html = renderToStaticMarkup(
      <EnrichmentGaps
        gaps={[{ ...gap, code: "search_failed", analysis_current: true }]}
        disabled={false}
        onAction={() => {}}
      />,
    );
    expect(html).toContain("Задача обновлена");
    expect(html).not.toContain("Анализ обновлён");
  });
  it("shows partial ASR coverage without claiming filled lyrics", () => {
    const html = renderToStaticMarkup(
      <EnrichmentGaps
        gaps={[{ ...gap, field: "synced_lyrics", last_alignment_coverage: 0.25 }]}
        disabled={false}
        onAction={() => {}}
      />,
    );
    expect(html).toContain("25% строк");
    expect(html).toContain("надёжные таймкоды не получены");
  });
});
