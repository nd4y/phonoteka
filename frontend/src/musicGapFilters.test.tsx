import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { TrackFilters } from "./TrackFilters";
import { parseView } from "./viewState";

it("offers and restores a field filter scoped to individual music recordings", () => {
  const filter = "music_missing_lyrics";
  const view = parseView(
    JSON.stringify({ page: "library", query: "Album", filter, pageNumber: 2 }),
  );
  const markup = renderToStaticMarkup(
    <TrackFilters filter={view.filter} search={view.query} onSearch={vi.fn()} onFilter={vi.fn()} />,
  );
  expect(view.pageNumber).toBe(2);
  expect(markup).toContain('<optgroup label="Пробелы в отдельных музыкальных записях">');
  expect(markup).toMatch(/<option(?=[^>]*value="music_missing_lyrics")(?=[^>]*selected="")[^>]*>/);
});
