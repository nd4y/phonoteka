import { useEffect, useRef, useState } from "react";
import { api, messageFor, requestId } from "./api";
import type { Job } from "./types";
import "./ConfirmedFieldsPanel.css";

type Change = { field: string; before: string | boolean; after: string | boolean; source: string };
type Pending = { field: string; code: string; message: string };
type Entry = {
  track_id: string;
  relative_path: string;
  title: string;
  artist: string;
  token: string;
  ready: boolean;
  changes: Change[];
  remaining: Pending[];
  blocked: string[];
  publication: { eligible: boolean; reason: string };
};
type Page = { items: Entry[]; next_after: string | null; scanned: number; total: number };
const names: Record<string, string> = {
  title: "Название",
  artist: "Исполнитель",
  album: "Альбом",
  cover: "Обложка",
  synced_lyrics: "Текст с таймкодами",
};
const display = (value: string | boolean) =>
  typeof value === "boolean" ? (value ? "Есть" : "Не заполнено") : value || "Не заполнено";

export function ConfirmedFieldsPanel({
  onRefresh,
  onJobs,
  onOpen,
}: {
  onRefresh: () => void;
  onJobs: () => void;
  onOpen: (id: string) => void;
}) {
  const [items, setItems] = useState<Entry[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [progress, setProgress] = useState({ scanned: 0, total: 0 });
  const [scanning, setScanning] = useState(false),
    [complete, setComplete] = useState(false);
  const [busy, setBusy] = useState(false),
    [submitted, setSubmitted] = useState(false);
  const [publish, setPublish] = useState(true),
    [visible, setVisible] = useState(30);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const serial = useRef(0),
    cancelled = useRef(false),
    lock = useRef(false);
  const keys = useRef(new Map<string, string>());
  useEffect(
    () => () => {
      serial.current++;
      cancelled.current = true;
    },
    [],
  );

  async function scan() {
    if (lock.current) return;
    lock.current = true;
    cancelled.current = false;
    const version = ++serial.current;
    setScanning(true);
    setComplete(false);
    setSubmitted(false);
    setError("");
    setNotice("");
    setItems([]);
    setSelected(new Set());
    setVisible(30);
    setProgress({ scanned: 0, total: 0 });
    keys.current.clear();
    const collected: Entry[] = [];
    const cursors = new Set<string>();
    let after = "";
    try {
      do {
        const page = await api<Page>(
          `/confirmed-fields/plan?limit=30${after ? `&after=${encodeURIComponent(after)}` : ""}`,
        );
        if (version !== serial.current || cancelled.current) break;
        if (
          collected.length + page.items.length > 100000 ||
          (page.next_after && cursors.has(page.next_after))
        ) {
          throw new Error("Не удалось закончить проверку коллекции. Обновите план.");
        }
        if (page.next_after) cursors.add(page.next_after);
        collected.push(...page.items);
        setProgress({ scanned: collected.length, total: page.total });
        after = page.next_after || "";
      } while (after);
      if (version !== serial.current) return;
      setItems(collected);
      setSelected(new Set(collected.filter((i) => i.ready).map((i) => i.track_id)));
      setComplete(!cancelled.current && !after);
      if (cancelled.current)
        setNotice("Проверка остановлена. Для записи нужен полный актуальный план.");
    } catch (err) {
      if (version === serial.current) setError(messageFor(err));
    } finally {
      if (version === serial.current) {
        lock.current = false;
        setScanning(false);
      }
    }
  }

  async function apply() {
    if (lock.current || !complete || submitted || !selected.size) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const version = serial.current;
    const chosen = items.filter((i) => i.ready && selected.has(i.track_id));
    let queued = 0;
    try {
      for (let offset = 0; offset < chosen.length; offset += 100) {
        if (version !== serial.current) return;
        const batch = chosen.slice(offset, offset + 100);
        const body = JSON.stringify({
          kind: "apply",
          track_ids: batch.map((i) => i.track_id),
          params: {
            acceptance_mode: "confirmed_fields",
            field_tokens: Object.fromEntries(batch.map((i) => [i.track_id, i.token])),
            publish_confirmed: publish,
          },
        });
        if (!keys.current.has(body)) keys.current.set(body, requestId());
        await api<Job>("/jobs", {
          method: "POST",
          body,
          headers: { "Idempotency-Key": keys.current.get(body)! },
        });
        if (version !== serial.current) return;
        queued += batch.length;
        setNotice(
          `Передано в очередь записи: ${queued}. Результаты записи и Ready будут в обработке.`,
        );
      }
      setSubmitted(true);
      onRefresh();
    } catch (err) {
      if (version === serial.current)
        setError(`${queued ? `Передано файлов: ${queued}. ` : ""}${messageFor(err)}`);
    } finally {
      if (version === serial.current) {
        lock.current = false;
        setBusy(false);
      }
    }
  }

  const ready = items.filter((i) => i.ready);
  const pending = new Map<string, { field: string; message: string; count: number }>();
  for (const item of items)
    for (const reason of item.remaining) {
      const key = `${reason.field}:${reason.code}`,
        old = pending.get(key);
      pending.set(key, {
        field: reason.field,
        message: reason.message,
        count: (old?.count || 0) + 1,
      });
    }
  const waiting = items.filter((i) => !i.ready && i.remaining.length);
  return (
    <section className="confirmed-fields" aria-label="Подтверждённое заполнение коллекции">
      <div className="panel-heading">
        <div>
          <h3>Заполнить подтверждённые поля</h3>
          <p>
            Теги и обложки можно заполнить, пока текст ещё проверяется. Заполненные значения и
            закреплённые поля сохраняются.
          </p>
          <p className="muted small">
            План охватывает всю коллекцию. Миксы и разговорные записи пропускаются.
          </p>
        </div>
        <button
          className="button secondary"
          disabled={scanning || busy}
          onClick={() => void scan()}
        >
          {scanning ? "Проверяем коллекцию…" : "Проверить коллекцию"}
        </button>
      </div>
      {scanning && (
        <div role="status">
          Проверено файлов: {progress.scanned} из {progress.total || "…"}
          <progress
            max={progress.total || 1}
            value={progress.scanned}
            aria-label="Проверка подтверждённых полей"
          />
          <button
            className="button subtle"
            onClick={() => {
              cancelled.current = true;
            }}
          >
            Остановить проверку
          </button>
        </div>
      )}
      {complete && (
        <>
          <p role="status">
            Проверено записей: {progress.scanned}. Доступно для заполнения: {ready.length}.
          </p>
          {!!ready.length && (
            <>
              <label className="confirmed-choice">
                <input
                  type="checkbox"
                  disabled={busy || submitted}
                  checked={selected.size === ready.length}
                  onChange={(e) =>
                    setSelected(new Set(e.target.checked ? ready.map((i) => i.track_id) : []))
                  }
                />
                Выбрать все подходящие записи
              </label>
              <div className="confirmed-list">
                {ready.slice(0, visible).map((item) => (
                  <article key={item.track_id}>
                    <label className="confirmed-choice">
                      <input
                        type="checkbox"
                        aria-label={`Выбрать запись ${item.relative_path}`}
                        disabled={busy || submitted}
                        checked={selected.has(item.track_id)}
                        onChange={(e) =>
                          setSelected((old) => {
                            const next = new Set(old);
                            if (e.target.checked) next.add(item.track_id);
                            else next.delete(item.track_id);
                            return next;
                          })
                        }
                      />
                      <strong>{item.title || item.relative_path}</strong>
                      {item.artist && <span>{item.artist}</span>}
                    </label>
                    <p className="muted small confirmed-path">{item.relative_path}</p>
                    <dl>
                      {item.changes.map((change) => (
                        <div key={change.field}>
                          <dt>{names[change.field] || change.field}</dt>
                          <dd>
                            {display(change.before)} → {display(change.after)}{" "}
                            <small className="muted">{change.source}</small>
                          </dd>
                        </div>
                      ))}
                    </dl>
                    {!!item.remaining.length && (
                      <details>
                        <summary>Осталось проверить: {item.remaining.length}</summary>
                        <ul>
                          {item.remaining.map((reason) => (
                            <li key={`${reason.field}:${reason.code}`}>
                              <strong>{names[reason.field] || reason.field}</strong> -{" "}
                              {reason.message}
                            </li>
                          ))}
                        </ul>
                        <button className="button subtle" onClick={() => onOpen(item.track_id)}>
                          Открыть запись
                        </button>
                      </details>
                    )}
                    {!item.publication.eligible && (
                      <p className="muted small">{item.publication.reason}</p>
                    )}
                  </article>
                ))}
              </div>
              {visible < ready.length && (
                <button className="button subtle" onClick={() => setVisible((n) => n + 30)}>
                  Показать ещё записи
                </button>
              )}
              <label className="confirmed-choice">
                <input
                  type="checkbox"
                  checked={publish}
                  disabled={busy || submitted}
                  onChange={(e) => setPublish(e.target.checked)}
                />
                Обновить готовую коллекцию по уже принятому профилю
              </label>
              <p className="muted small">
                Сохраняются прежние решения по неизменным аудио и тексту. Без принятого профиля
                изменения останутся в рабочем файле. Новые слова, таймкоды и прослушивание не
                подтверждаются этим действием.
              </p>
              <button
                className="button primary"
                disabled={busy || submitted || !selected.size}
                onClick={() => void apply()}
              >
                {busy
                  ? "Передаём в обработку…"
                  : submitted
                    ? "Передано в обработку"
                    : `Заполнить подтверждённое · ${selected.size}`}
              </button>
              {submitted && (
                <p className="muted small">
                  После выполнения обновите план, чтобы увидеть оставшиеся поля.
                </p>
              )}
            </>
          )}
          {!ready.length && (
            <p>
              Сейчас нет полей, которые проходят автоматическую проверку. Причины ниже; это не
              означает, что данные невозможно найти.
            </p>
          )}
          {!!pending.size && (
            <details>
              <summary>Что остаётся на проверке</summary>
              <p className="muted small">
                Счётчики относятся к отдельным полям и могут включать один файл несколько раз.
              </p>
              <ul>
                {[...pending].map(([key, reason]) => (
                  <li key={key}>
                    <strong>
                      {names[reason.field] || reason.field}: {reason.count}
                    </strong>{" "}
                    - {reason.message}
                  </li>
                ))}
              </ul>
              <details>
                <summary>Примеры записей</summary>
                {waiting.slice(0, 30).map((item) => (
                  <p key={item.track_id}>
                    <button className="button subtle" onClick={() => onOpen(item.track_id)}>
                      {item.title || item.relative_path}
                    </button>{" "}
                    {item.remaining[0].message}
                  </p>
                ))}
              </details>
            </details>
          )}
        </>
      )}
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
    </section>
  );
}
