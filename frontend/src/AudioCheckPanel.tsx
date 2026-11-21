import { useEffect, useRef, useState } from "react";
import { api, messageFor } from "./api";

type Item = {
  id: string;
  file: string;
  state: string;
  token?: string;
  language?: string | null;
  anchored?: number;
  lines?: number;
  median_ms?: number | null;
};
type Page = { items: Item[]; offset: number; next_offset: number | null };
const reasons: Record<string, string> = {
  ready: "Ожидает проверки по аудио",
  no_match: "Нет подходящей версии текста с разрешением на сверку",
  blocked: "Проверка недоступна для этой записи",
  changed: "Файл изменился после поиска",
  attempted: "Откройте предыдущую задачу в обработке",
  busy: "У записи есть незавершённая задача",
  checked_empty: "ASR завершился без размеченных слов",
};

export function AudioCheckPanel({ id, onJobs }: { id: string; onJobs: () => void }) {
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [queued, setQueued] = useState(0);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const ready = page?.items.filter((item) => item.state === "ready" && item.token) || [];
  const compared = page?.items.filter((item) => item.lines !== undefined).length || 0;

  async function request(offset: number, submit = false) {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    const url = "/preparation/" + encodeURIComponent(id) + "/audio-check?offset=" + offset;
    try {
      if (submit) {
        const result = await api<{ total: number }>(url, {
          method: "POST",
          body: JSON.stringify({
            tokens: Object.fromEntries(ready.map((item) => [item.id, item.token])),
          }),
          signal: controller.signal,
        });
        if (!controller.signal.aborted) setQueued(result.total);
      }
      const checked = await api<Page>(url, { signal: controller.signal });
      if (!controller.signal.aborted) setPage(checked);
    } catch (e) {
      if (!controller.signal.aborted) setError(messageFor(e));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      pending.current = null;
    }
  }
  return (
    <section aria-label="Сверка вариантов по аудио">
      <div className="button-row">
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => void request(page?.offset || 0)}
        >
          {busy ? "Проверяем…" : page ? "Обновить сверку" : "Сверить варианты по аудио"}
        </button>
        {ready.length > 0 && page && (
          <button
            className="button primary"
            disabled={busy}
            onClick={() => void request(page.offset, true)}
          >
            Запустить ASR для {ready.length} записей
          </button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      {queued > 0 && (
        <p role="status">
          В задаче ASR записей: {queued}.{" "}
          <button className="button subtle" onClick={onJobs}>
            Открыть обработку
          </button>
        </p>
      )}
      {page && (
        <>
          <p>
            Сверены с аудио: {compared} из {page.items.length} на этой странице. Для {ready.length}{" "}
            доступна новая расшифровка.
          </p>
          <p>
            Фонотека проверяет строки по расшифровке текущей версии файла. Для русского языка на PC2
            используется настроенный GigaAM. Запись таймкодов требует отдельного подтверждения.
          </p>
          <details>
            <summary>Результаты по файлам ({page.items.length})</summary>
            <ul>
              {page.items.map((item) => (
                <li key={item.id}>
                  <strong>{item.file}</strong>
                  {" - "}
                  {item.lines !== undefined
                    ? `Сопоставлено строк: ${item.anchored} из ${item.lines}${item.median_ms != null ? `. Медианное расхождение: ${(item.median_ms / 1000).toLocaleString("ru", { maximumFractionDigits: 2 })} с` : ""}. ${item.state === "matched" ? "Проверьте точность таймкодов." : "Таймкоды требуют проверки."}`
                    : reasons[item.state] || "Требуется проверка"}
                </li>
              ))}
            </ul>
          </details>
          <div className="button-row">
            {page.offset > 0 && (
              <button
                className="button subtle"
                disabled={busy}
                onClick={() => void request(Math.max(0, page.offset - 60))}
              >
                Предыдущие записи
              </button>
            )}
            {page.next_offset !== null && (
              <button
                className="button subtle"
                disabled={busy}
                onClick={() => void request(page.next_offset!)}
              >
                Следующие записи
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
