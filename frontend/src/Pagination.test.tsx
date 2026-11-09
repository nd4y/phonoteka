import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { Pagination } from "./Pagination";
import type { Track, TrackPage } from "./types";

it("announces the new range only once its rows have arrived", () => {
  const firstPage: TrackPage = { items: [{} as Track], total: 61, page: 1, page_size: 30 };
  const onPage = () => {};
  for (const compact of [false, true]) {
    const pending = renderToStaticMarkup(
      <Pagination tracks={firstPage} page={2} onPage={onPage} loading={false} compact={compact} />,
    );
    expect(pending).toContain("Загрузка каталога");
    expect(pending.match(/<button[^>]* disabled=""/g)).toHaveLength(2);
    const loaded = renderToStaticMarkup(
      <Pagination
        tracks={{ ...firstPage, page: 2 }}
        page={2}
        onPage={onPage}
        loading={false}
        compact={compact}
      />,
    );
    expect(loaded).toContain(compact ? "2 / 3" : "Показано 31-60 из 61");
    expect(loaded).not.toContain("Загрузка каталога");
  }
});
