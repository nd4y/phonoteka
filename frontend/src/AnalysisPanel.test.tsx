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
const render = (value: Track) =>
  renderToStaticMarkup(
    <AnalysisPanel
      track={value}
      enabled
      recognitionEnabled
      asrEnabled={false}
      busy={false}
      changed={false}
      onJob={async () => {}}
      onRefresh={() => {}}
    />,
  );
describe("source search presentation", () => {
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
    expect(html).toContain("Проверить выбранные изменения");
  });
});
