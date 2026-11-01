import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCheck,
  FolderCheck,
  RefreshCw,
  Search,
  TriangleAlert,
} from "lucide-react";
import { api, messageFor } from "./api";
import type { Job } from "./types";
import { nextCollectionAction, publicationProgress } from "./workflow";
import type { WorkflowAction } from "./workflow";

type Report = { counts: Record<string, number>; generated_at?: string };
const labels: Record<WorkflowAction, [string, string, string]> = {
  guide: [
    "Подготовим вашу музыку",
    "Мастер покажет доступную папку и поможет найти недостающие данные. Найденные варианты вы проверите перед записью.",
    "Продолжить с мастером",
  ],
  jobs: [
    "Коллекция обрабатывается",
    "Посмотрите текущие действия и оставшееся время. Пока задача выполняется, можно просматривать и слушать музыку.",
    "Открыть обработку",
  ],
  review: [
    "Проверьте найденные данные",
    "Сравните изменения с файлом, прослушайте запись и примените подходящие значения. Готовую коллекцию сохраним отдельно.",
    "Открыть очередь проверки",
  ],
  errors: [
    "Несколько файлов требуют внимания",
    "Посмотрите причины ошибок чтения или проверки. Повторный поиск тегов не исправляет недоступное аудио.",
    "Разобрать ошибки",
  ],
  ready: [
    "Готовые файлы доступны",
    "Откройте результат или подготовьте снимок с аудио и плейлистом для переноса на телефон.",
    "Открыть готовые файлы",
  ],
};

export function WorkflowOverview({
  jobs,
  onAction,
}: {
  jobs: Job[];
  onAction: (action: WorkflowAction) => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const value = await api<Report>("/reports/collection", {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) {
        setReport(value);
        setError("");
      }
    } catch (problem) {
      if (!controller.signal.aborted) setError(messageFor(problem));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);
  const jobStates = jobs.map((job) => `${job.id}:${job.state}`).join("|");
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load, jobStates]);
  const counts = report?.counts || {};
  const { ready, total, percent } = publicationProgress(counts);
  const next = nextCollectionAction(counts, jobs);
  const [title, detail, action] = labels[next];
  const n = (value: number | null | undefined) =>
    value == null ? "-" : value.toLocaleString("ru-RU");
  return (
    <section className="workflow-overview" aria-label="Обзор коллекции" aria-busy={loading}>
      <div className="workflow-freshness">
        <span>
          {report?.generated_at
            ? `Снимок: ${new Date(report.generated_at).toLocaleString("ru-RU")}`
            : "Получаем состояние коллекции"}
        </span>
        <button className="text-button" disabled={loading} onClick={() => void load()}>
          <RefreshCw size={16} />
          Обновить
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error} {report && "Показан предыдущий снимок."}
        </div>
      )}
      {!report ? (
        <p role="status">
          {error ? "Не удалось прочитать состояние. Повторите обновление." : "Загрузка..."}
        </p>
      ) : (
        <>
          <div className="workflow-next">
            <div>
              <div className="eyebrow">ВАШ СЛЕДУЮЩИЙ ШАГ</div>
              <h2>{title}</h2>
              <p>{detail}</p>
              <button className="button primary" onClick={() => onAction(next)}>
                {action}
                <ArrowRight size={18} />
              </button>
            </div>
            <div className="workflow-result">
              <strong>
                {n(ready)} <small>из {n(total)}</small>
              </strong>
              <span>
                аудиофайлов сохранены
                <br />в готовую коллекцию
              </span>
              {percent !== null && (
                <progress aria-label="Аудиофайлы в готовой коллекции" value={ready!} max={total!} />
              )}
              <small>Доставка на телефон - следующий шаг</small>
            </div>
          </div>
          <div className="workflow-attention">
            <button onClick={() => onAction("review")}>
              <CheckCheck size={22} />
              <strong>{n(counts.current_proposals)}</strong>
              <span>
                Сохраненные предложения
                <small>Открыть всю очередь проверки, включая ошибки</small>
              </span>
            </button>
            <button onClick={() => onAction("guide")}>
              <Search size={22} />
              <strong>{n(counts.no_match)}</strong>
              <span>Поиск без совпадений</span>
            </button>
            <button onClick={() => onAction("errors")}>
              <TriangleAlert size={22} />
              <strong>{n(counts.audit_errors)}</strong>
              <span>Ошибки проверки аудио</span>
            </button>
            <button onClick={() => onAction("ready")}>
              <FolderCheck size={22} />
              <strong>{n(ready)}</strong>
              <span>Готовые файлы</span>
            </button>
          </div>
          <p className="muted small">
            Состояния могут пересекаться. Наличие тегов или текста не означает проверку их
            правильности. CUE: {n(counts.cue_sheets)} документов с границами композиций;
            аудиофайлов: {n(counts.audio_files)}.
          </p>
          <details className="workflow-explainer">
            <summary>Что считается готовым?</summary>
            <p>
              Текущая версия сохранена в каталог результата с проверками выбранного профиля.
              Предложение, запись тегов в файл и сохранение готовой коллекции - разные этапы. Доля
              относится к аудиофайлам каталога, без документов CUE, а не к длительности музыки или
              качеству таймкодов.
            </p>
            <p>
              Для инструментальных записей решение об отсутствии текста принимается явно в карточке.
              Нераспознанное аудио и незаполненные поля остаются видимыми в подробном отчете.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
