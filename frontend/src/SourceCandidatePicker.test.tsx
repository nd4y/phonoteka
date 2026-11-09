import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SourceCandidatePicker, SourceSelectionComparison } from "./SourceCandidatePicker";
import { chooseCandidate, groupCandidates, lyricsSummary } from "./sourceCandidates";
import type { Candidate, Track } from "./types";

const candidates: Candidate[] = [
  {
    id: "mb",
    kind: "metadata",
    provider: "musicbrainz",
    tags: { title: "Song", album: "Studio" },
    duration: 180,
    match: {
      state: "candidate",
      reasons: ["duration_differs"],
      duration_delta: 15,
    },
  },
  {
    id: "shazam",
    kind: "metadata",
    provider: "shazam",
    tags: { title: "Song", album: "Live" },
    match: { state: "candidate", reasons: ["recording_version_unverified"] },
  },
  {
    id: "lyrics",
    kind: "lyrics",
    provider: "lrclib",
    title: "Song",
    plain_lyrics: "One\nTwo",
    match: { state: "candidate", reasons: [] },
  },
];
const track: Track = {
  id: "demo",
  sha256: "a",
  revision: "a",
  format: "mp3",
  relative_path: "demo.mp3",
  tags: { title: "Song", artist: "Artist" },
  has_art: false,
  status: "ready",
  can_write: true,
  analysis: {
    input_sha256: "a",
    query_source: "embedded",
    state: "candidate",
    sources: [],
    candidates,
  },
};

describe("source selection decisions", () => {
  it("identifies cover editions using public release details and the embedded release ID", () => {
    const cover = {
      id: "cover",
      kind: "cover",
      provider: "coverartarchive",
      release_id: "edition-one",
      match: { state: "needs_review", reasons: ["confirm_release"] },
    };
    const html = renderToStaticMarkup(
      <SourceCandidatePicker
        track={{
          ...track,
          tags: { ...track.tags, musicbrainz_albumid: "EDITION-ONE" },
          analysis: {
            ...track.analysis!,
            candidates: [
              {
                ...candidates[0],
                releases: [
                  { id: "edition-one", title: "Studio deluxe", date: "2024-10-01", country: "GB" },
                ],
              },
              cover,
              { ...cover, id: "another-cover", release_id: "edition-two" },
            ],
          },
        }}
        changed={false}
        onRefresh={() => {}}
      />,
    );
    expect(html).toContain("Studio deluxe");
    expect(html).toContain("2024-10-01 · GB");
    expect(html).toContain("ID издания совпадает с тегом файла.");
    expect(html).toContain("edition-two");
    expect(html).toContain("Проверьте релиз");
  });
  it("separates purposes without hiding competing versions or implying plain text has timing", () => {
    expect(groupCandidates(candidates).map((g) => [g.kind, g.candidates.length])).toEqual([
      ["metadata", 2],
      ["lyrics", 1],
    ]);
    const html = renderToStaticMarkup(
      <SourceCandidatePicker track={track} changed={false} onRefresh={() => {}} />,
    );
    for (const value of [
      "Теги песни",
      "Текст песни",
      "Studio",
      "Live",
      "3:00",
      "разница 15.0 с",
      "Без таймкодов",
      "Сравнить изменения",
    ])
      expect(html).toContain(value);
    expect(html).not.toContain('type="checkbox"');
    expect(html).not.toContain("Сохранить как предложение");
    expect(html).toContain("версия и альбом не подтверждены");
  });
  it("replacing a metadata choice preserves the chosen text, while skipping only removes that group", () => {
    expect(chooseCandidate(["mb", "lyrics"], candidates, "metadata", "shazam")).toEqual([
      "lyrics",
      "shazam",
    ]);
    expect(chooseCandidate(["mb", "lyrics"], candidates, "metadata")).toEqual(["lyrics"]);
  });
  it("shows actual source priority result, omitted fields and replacement of the whole prior draft", () => {
    const html = renderToStaticMarkup(
      <SourceSelectionComparison
        preview={{
          preview_revision: "x",
          blocked_fields: ["title"],
          omitted_fields: ["lrc"],
          replaces_proposal: true,
          fields: [
            {
              field: "album",
              before: "Before",
              after: "After",
              changed: true,
              source: { provider: "musicbrainz" },
            },
          ],
        }}
      />,
    );
    for (const value of [
      "Before",
      "After",
      "MusicBrainz",
      "Закреплены и сохранятся",
      "Не войдут в предложение",
      "заменит текущее предложение целиком",
    ])
      expect(html).toContain(value);
  });
  it("distinguishes mixed timing from a complete timed text", () => {
    expect(lyricsSummary("[00:01]One\nTwo")).toContain("Часть строк");
    expect(lyricsSummary("[ar:Artist]\n[00:01]One\n[00:03]Two")).toBe("С таймкодами · строк: 2");
  });
  it("disables choices for stale analysis or unsaved edits", () => {
    const html = renderToStaticMarkup(
      <SourceCandidatePicker
        track={{ ...track, analysis: { ...track.analysis!, stale: true } }}
        changed={false}
        onRefresh={() => {}}
      />,
    );
    expect(html).toContain('class="source-group" disabled=""');
  });
});
