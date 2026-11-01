import { useLayoutEffect, useRef, useState } from "react";
import { ArrowDown, Pause, Play, Search } from "lucide-react";
import { jobStates } from "./jobs";
import type { ActivityEvent } from "./jobs";
import { filterJobEvents, isProblemEvent } from "./jobLog";

export function ActivityLog({ events }: { events: ActivityEvent[] }) {
  const list = useRef<HTMLOListElement>(null);
  // Freeze the bounded visible window when reading. The SSE feed keeps running;
  // eviction of old feed rows must not displace the reader's scroll position.
  const [snapshot, setSnapshot] = useState<ActivityEvent[] | null>(null);
  const [query, setQuery] = useState("");
  const [problems, setProblems] = useState(false);
  const following = snapshot === null;
  const loaded = snapshot || events;
  const visible = filterJobEvents(loaded, query, problems);
  const unread = snapshot
    ? events.filter((event) => event.id > (snapshot.at(-1)?.id || 0)).length
    : 0;
  function pause() {
    if (following) setSnapshot(events.slice());
  }
  useLayoutEffect(() => {
    if (following && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [events, following, query, problems]);
  return (
    <section className="job-log-wrap" aria-label="Журнал событий">
      <div className="job-log-heading">
        <h4>Журнал событий</h4>
        <span className="muted small">
          {visible.length} из {loaded.length} загруженных
        </span>
      </div>
      <div className="job-log-toolbar">
        <label className="job-log-search">
          <Search size={17} aria-hidden="true" />
          <span className="sr-only">Поиск в загруженных событиях</span>
          <input
            type="search"
            value={query}
            placeholder="Файл, действие или код ошибки"
            onChange={(event) => {
              pause();
              setQuery(event.target.value);
            }}
          />
        </label>
        <button
          className="button secondary"
          aria-pressed={problems}
          onClick={() => {
            pause();
            setProblems((value) => !value);
          }}
        >
          {`Только проблемы (${loaded.filter(isProblemEvent).length})`}
        </button>
        <button
          className="button secondary"
          aria-label={following ? "Приостановить прокрутку журнала" : "Следить за новыми событиями"}
          onClick={() => (following ? pause() : setSnapshot(null))}
        >
          {following ? <Pause size={16} /> : <Play size={16} />}
          {following ? "Пауза прокрутки" : "Следить за новыми"}
        </button>
      </div>
      <ol
        className="job-event-log"
        role="log"
        aria-label="События задачи"
        aria-live="off"
        tabIndex={0}
        ref={list}
        onScroll={() => {
          const node = list.current;
          if (following && node && node.scrollHeight - node.scrollTop - node.clientHeight > 32)
            pause();
        }}
      >
        {visible.map((event) => (
          <li
            key={event.id}
            data-event-id={event.id}
            className={
              isProblemEvent(event)
                ? event.state === "needs_review" ||
                  event.state === "waiting_authorization" ||
                  event.state === "deferred"
                  ? "event-attention"
                  : "event-error"
                : ""
            }
          >
            <time
              dateTime={event.created_at}
              title={new Date(event.created_at).toLocaleString("ru-RU")}
            >
              {new Date(event.created_at).toLocaleTimeString("ru-RU", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })}
            </time>
            <div>
              {event.filename && <strong>{event.filename}</strong>}
              <span>{event.message}</span>
              {event.state && (
                <small className="job-event-state">{jobStates[event.state] || event.state}</small>
              )}
              {event.code && (
                <small>
                  Код: <code>{event.code}</code>
                </small>
              )}
              {event.retry_at && (
                <small>
                  Повтор после {new Date(event.retry_at * 1000).toLocaleString("ru-RU")}
                </small>
              )}
            </div>
          </li>
        ))}
        {!visible.length && (
          <li className="job-log-empty">
            {!following && !loaded.length
              ? "На момент паузы событий не было. Нажмите «К последним», чтобы обновить журнал."
              : loaded.length
                ? "В загруженных событиях ничего не найдено. Измените поиск или фильтр."
                : "События появятся при выполнении задачи."}
          </li>
        )}
      </ol>
      <div className="job-log-footer">
        <p className="muted small job-log-note">
          {following
            ? "Новые события появляются внизу. Прокрутите вверх, чтобы спокойно читать."
            : "Прокрутка приостановлена. Обработка продолжается."}{" "}
          Доступны последние 200 событий; поиск работает только в загруженном окне.
        </p>
        {!following && (
          <button className="button secondary" onClick={() => setSnapshot(null)}>
            <ArrowDown size={16} />К последним{unread ? ` · новых в окне: ${unread}` : ""}
          </button>
        )}
      </div>
    </section>
  );
}
