import { ArrowRight, CircleHelp } from "lucide-react";
import type { EnrichmentGap } from "./types";
import "./EnrichmentGaps.css";

const labels: Record<EnrichmentGap["field"], string> = {
  title: "Название",
  artist: "Исполнитель",
  album: "Альбом",
  cover: "Обложка",
  synced_lyrics: "Текст с таймкодами",
};
const actions: Record<EnrichmentGap["action"], string> = {
  sources: "Открыть источники",
  review: "Проверить предложение",
  lyrics: "Открыть текст",
  settings: "Открыть настройки",
  search: "Повторить поиск",
  recognize: "Распознать запись",
};

const focusedFields: Partial<Record<string, EnrichmentGap["field"]>> = {
  missing_art: "cover",
  music_missing_art: "cover",
  missing_lyrics: "synced_lyrics",
  music_missing_lyrics: "synced_lyrics",
};

export function GapSummary({ gaps = [], filter }: { gaps?: EnrichmentGap[]; filter?: string }) {
  const field = filter ? focusedFields[filter] : undefined;
  const gap = field ? gaps.find((item) => item.field === field) : gaps[0];
  if (!gap) return null;
  return (
    <span className="gap-summary">
      <CircleHelp size={13} aria-hidden="true" />
      <span>
        {labels[gap.field]}: {gap.message}
        {gaps.length > 1 && <span className="gap-more"> Ещё {gaps.length - 1}</span>}
      </span>
    </span>
  );
}

export function EnrichmentGaps({
  gaps = [],
  disabled,
  onAction,
}: {
  gaps?: EnrichmentGap[];
  disabled: boolean;
  onAction: (gap: EnrichmentGap) => void;
}) {
  if (!gaps.length) return null;
  return (
    <section className="enrichment-gaps" aria-label="Что осталось заполнить">
      <h3>Что осталось заполнить</h3>
      <p className="muted small">
        Причины относятся к данным внутри файла. Найденный вариант ещё нужно проверить и записать.
      </p>
      <div className="enrichment-gap-list">
        {gaps.map((gap) => (
          <div className="enrichment-gap" key={gap.field}>
            <div className="enrichment-gap-label">{labels[gap.field]}</div>
            <div className="enrichment-gap-body">
              <p>{gap.message}</p>
              <p className="muted small">{gap.next_step}</p>
              {gap.last_alignment_coverage !== undefined && (
                <p className="muted small">
                  Предыдущее выравнивание ASR: {Math.round(gap.last_alignment_coverage * 100)}%
                  строк.
                  {gap.last_alignment_coverage < 1
                    ? " Для остальных строк надёжные таймкоды не получены."
                    : " Времена ещё требуют проверки по вокалу."}
                </p>
              )}
              <span className="enrichment-gap-evidence">
                {gap.provider && <span>{gap.provider}</span>}
                {gap.checked_at && (
                  <span>
                    {gap.code === "search_failed"
                      ? "Задача обновлена"
                      : gap.analysis_current
                        ? "Анализ обновлён"
                        : "Прежний анализ"}
                    : {new Date(gap.checked_at).toLocaleString("ru-RU")}
                  </span>
                )}
                {!!gap.candidate_count && <span>Вариантов: {gap.candidate_count}</span>}
              </span>
            </div>
            <button className="button subtle" disabled={disabled} onClick={() => onAction(gap)}>
              {gap.code === "not_searched" && gap.action === "search"
                ? "Найти данные"
                : actions[gap.action]}
              <ArrowRight size={15} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
