import { useEffect, useRef, useState } from "react";
import { api, messageFor, requestId } from "./api";
import { AlbumBatchPanel } from "./AlbumBatchPanel";
import type { Job } from "./types";
import "./ReviewQueuePanel.css";

export type ReviewCategory = "prepare" | "choose" | "listen" | "file";
export const reviewLabels: Record<ReviewCategory, string> = {
  prepare: "Подготовка",
  choose: "Выбрать вариант",
  listen: "Прослушать",
  file: "Проблема файла",
};
type Batch = {
  kind: "recognize" | "align";
  total: number;
  items: { track_id: string; token: string }[];
};
type Plan = {
  counts: Record<ReviewCategory | "total", number>;
  batches: Batch[];
  albums: { id: string; version: number; title: string; count: number; track_ids: string[] }[];
};

export function ReviewQueuePanel({
  filter,
  refreshKey,
  onFilter,
  onRefresh,
  onOpen,
  onJobs,
}: {
  filter: string;
  refreshKey: string;
  onFilter: (value: string) => void;
  onRefresh: () => void;
  onOpen: (id: string) => void;
  onJobs: () => void;
}) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [album, setAlbum] = useState<string | null>(null);
  const keys = useRef(new Map<string, string>());
  const lock = useRef(false);
  const serial = useRef(0);
  async function load() {
    const version = ++serial.current;
    try {
      const value = await api<Plan>("/review/plan");
      if (version === serial.current) {
        setPlan(value);
        setError("");
      }
    } catch (err) {
      if (version === serial.current) setError(messageFor(err));
    }
  }
  useEffect(() => {
    void load();
    return () => {
      serial.current++;
    };
  }, [refreshKey]);
  const batches = plan?.batches.filter((b) => b.items.length) || [];
  const count = batches.reduce((sum, b) => sum + b.items.length, 0);
  async function prepare() {
    if (lock.current || !count) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    let started = 0;
    try {
      for (const batch of batches) {
        const body = {
          kind: batch.kind,
          track_ids: batch.items.map((i) => i.track_id),
          params: {
            review_tokens: Object.fromEntries(batch.items.map((i) => [i.track_id, i.token])),
          },
        };
        const identity = JSON.stringify(body);
        if (!keys.current.has(identity)) keys.current.set(identity, requestId());
        await api<Job>("/jobs", {
          method: "POST",
          body: identity,
          headers: { "Idempotency-Key": keys.current.get(identity)! },
        });
        started += batch.items.length;
        setNotice(`В подготовку передано: ${started}. Теги и таймкоды в файлы не записываются.`);
      }
    } catch (err) {
      setError(messageFor(err));
    } finally {
      await load();
      onRefresh();
      lock.current = false;
      setBusy(false);
    }
  }
  const selectedAlbum = plan?.albums.find((a) => a.id === album);
  return (
    <section className="panel review-queue-panel" aria-label="Разбор очереди проверки">
      <div className="panel-heading">
        <div>
          <h2>Что осталось проверить</h2>
          <p className="muted">
            Приложение готовит данные. Вы принимаете решения по спорным результатам.
          </p>
          <p className="muted small">
            Счётчики по всей коллекции. Поиск ниже дополнительно сужает список.
          </p>
        </div>
        <button
          className="button subtle"
          disabled={busy}
          onClick={() => {
            setError("");
            void load();
            onRefresh();
          }}
        >
          Обновить очередь
        </button>
      </div>
      <div className="review-categories" aria-label="Причины проверки">
        <button aria-pressed={filter === "review"} onClick={() => onFilter("review")}>
          Все причины <strong>{plan?.counts.total ?? "..."}</strong>
        </button>
        {(Object.keys(reviewLabels) as ReviewCategory[]).map((key) => (
          <button
            key={key}
            data-category={key}
            aria-pressed={filter === `review_${key}`}
            onClick={() => onFilter(`review_${key}`)}
          >
            {reviewLabels[key]} <strong>{plan?.counts[key] ?? "..."}</strong>
          </button>
        ))}
      </div>
      <div className="review-preparation">
        <div>
          <h3>
            {!plan
              ? error
                ? "Не удалось загрузить план"
                : "Загружаем план подготовки..."
              : count
                ? `Можно подготовить: ${count}`
                : "Доступных автоматических шагов сейчас нет"}
          </h3>
          <p className="muted small">
            {batches
              .map(
                (b) =>
                  `${b.kind === "align" ? "Подготовить таймкоды" : "Перепроверить состав записи"}: ${b.items.length}`,
              )
              .join(" · ") || "Откройте причину проверки. Запущенные задачи видны в обработке."}
          </p>
          <p className="muted small">
            Лимиты источников сохраняются. Каждая подготовка выполняется один раз для текущих
            данных; новые результаты остаются на проверке.
          </p>
        </div>
        <button className="button primary" disabled={busy || !count} onClick={() => void prepare()}>
          {busy ? "Передаём в обработку..." : "Подготовить доступное"}
        </button>
      </div>
      {notice && (
        <p role="status">
          {notice}{" "}
          <button className="button subtle" onClick={onJobs}>
            Открыть обработку
          </button>
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!!plan?.albums.length && (
        <details>
          <summary>Проверить общие данные альбома</summary>
          <p className="muted small">
            Только группы с подтверждённым составом релиза. Сначала сравните общие изменения; запись
            в файлы выполняется отдельно.
          </p>
          <div className="button-row">
            {plan.albums.map((a) => (
              <button key={a.id} className="button secondary" onClick={() => setAlbum(a.id)}>
                {a.title} · {a.count} записей
              </button>
            ))}
          </div>
          {selectedAlbum && (
            <AlbumBatchPanel
              ids={selectedAlbum.track_ids}
              reviewId={selectedAlbum.id}
              reviewVersion={selectedAlbum.version}
              onOpen={onOpen}
              onSaved={() => {
                void load();
                onRefresh();
              }}
            />
          )}
        </details>
      )}
    </section>
  );
}
