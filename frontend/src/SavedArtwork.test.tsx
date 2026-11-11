import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import SavedArtwork from "./SavedArtwork";

describe("saved artwork confirmation", () => {
  it("shows the exact saved proposal rather than the audio file's existing cover", () => {
    const html = renderToStaticMarkup(<SavedArtwork trackId="track/id" proposalId="draft/id" />);
    expect(html).toContain("/api/tracks/track%2Fid/proposals/draft%2Fid/art");
    expect(html).toContain("Обложка из сохранённого предложения");
    expect(html).toContain("будет записано в выбранный слот");
    expect(html).not.toContain("/api/tracks/track%2Fid/art");
  });
});
