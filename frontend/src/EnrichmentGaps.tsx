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

export function GapSummary({ gaps = [] }: { gaps?: EnrichmentGap[] }) {
  if (!gaps.length) return null;
  return (
    <span className="gap-summary">
      <CircleHelp size={13} aria-hidden="true" />
      <span>
        {labels[gaps[0].field]}: {gaps[0].message}
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
              <span className="enrichment-gap-evidence">
                {gap.provider && <span>{gap.provider}</span>}
                {gap.checked_at && (
                  <span>
                    {gap.analysis_current ? "Поиск" : "Прежний поиск"}:{" "}
                    {new Date(gap.checked_at).toLocaleString("ru-RU")}
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
