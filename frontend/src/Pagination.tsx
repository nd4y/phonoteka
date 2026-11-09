import { ChevronLeft, ChevronRight } from "lucide-react";
import type { TrackPage } from "./types";

export function Pagination({
  tracks,
  page,
  onPage,
  loading,
  compact = false,
}: {
  tracks: TrackPage | null;
  page: number;
  onPage: (page: number) => void;
  loading: boolean;
  compact?: boolean;
}) {
  const loadedPage = !loading && tracks?.page === page ? tracks : null;
  const pages = loadedPage ? Math.max(1, Math.ceil(loadedPage.total / loadedPage.page_size)) : 1;
  return (
    <div className={`pagination ${compact ? "compact" : ""}`}>
      <span className="muted small" aria-live="polite">
        {!loadedPage
          ? "Загрузка каталога"
          : compact
            ? `${page} / ${pages}`
            : `Показано ${loadedPage.items.length ? (page - 1) * loadedPage.page_size + 1 : 0}-${Math.min(page * loadedPage.page_size, loadedPage.total)} из ${loadedPage.total.toLocaleString("ru-RU")}`}
      </span>
      <div>
        <button
          className="icon-button"
          disabled={!loadedPage || page <= 1}
          onClick={() => onPage(page - 1)}
          aria-label="Предыдущая страница"
        >
          <ChevronLeft size={18} />
        </button>
        <button
          className="icon-button"
          disabled={!loadedPage || page >= pages}
          onClick={() => onPage(page + 1)}
          aria-label="Следующая страница"
        >
          <ChevronRight size={18} />
        </button>
      </div>
    </div>
  );
}
