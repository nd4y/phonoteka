import { useEffect, useState } from "react";
import { Bell, ChevronLeft, ChevronRight, Pause, Play, RefreshCw } from "lucide-react";
import { api, messageFor } from "./api";

type Delivery = {
  id: string;
  job_id: string;
  state: string;
  attempts: number;
  next_attempt: number;
  last_code?: string | null;
  created_at: string;
};
type Status = {
  enabled: boolean;
  configured: boolean;
  error?: string | null;
  host?: string;
  paused: boolean;
  recovery_suspended: boolean;
  counts: Record<string, number>;
  reserved_jobs: number;
  capacity: number;
  other_destination_pending: number;
  items: Delivery[];
};
const labels: Record<string, string> = {
  pending: "Ожидает доставки",
  in_flight: "Доставляется",
  delivered: "Доставлено",
  dead: "Окончательный отказ",
};

export function WebhooksPanel() {
  const [data, setData] = useState<Status | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  async function load() {
    setData(await api<Status>(`/webhooks?page=${page}&page_size=20`));
  }
  useEffect(() => {
    let stopped = false;
    async function refresh() {
      try {
        const result = await api<Status>(`/webhooks?page=${page}&page_size=20`);
        if (!stopped) {
          setData(result);
          setError("");
        }
      } catch (err) {
        if (!stopped) setError(messageFor(err));
      }
    }
    void refresh();
    const timer = setInterval(() => {
      void refresh();
    }, 10000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [page]);
  async function control(action: string, identity = "") {
    setBusy(identity || action);
    setError("");
    setNotice("");
    try {
      await api(
        identity ? `/webhooks/${encodeURIComponent(identity)}/retry` : `/webhooks/${action}`,
        { method: "POST" },
      );
      await load();
      setNotice(
        action === "retry"
          ? "Уведомление возвращено в очередь с тем же event ID. Обработка музыки не повторяется."
          : action === "pause"
            ? "Доставка уведомлений приостановлена. События сохраняются."
            : "Доставка уведомлений продолжена.",
      );
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy("");
    }
  }
  return (
    <section className="panel settings-panel settings-wide">
      <div className="section-intro">
        <h2>
          <Bell size={20} aria-hidden="true" /> Webhook уведомления
        </h2>
        <button
          className="icon-button"
          aria-label="Обновить уведомления"
          disabled={!!busy}
          onClick={() => {
            void load().catch((err) => setError(messageFor(err)));
          }}
        >
          <RefreshCw size={17} />
        </button>
      </div>
      <p className="muted small">
        Необязательная интеграция. Адрес получателя, разрешенный хост и HMAC-секрет задает оператор
        в Docker. Уведомления содержат только итог задачи, идентификаторы, счетчики и коды ошибок.
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      {data && (
        <>
          <p className="small" style={{ margin: "14px 0" }}>
            {data.enabled
              ? data.paused
                ? "Доставка на паузе"
                : "Доставка включена"
              : "Webhook выключен"}
            {data.host ? ` · ${data.host}` : ""}
          </p>
          {data.error && (
            <p className="warning-text small">
              Ошибка конфигурации: {data.error}. Музыкальные результаты сохраняются.
            </p>
          )}
          {data.recovery_suspended && (
            <p className="warning-text small">
              После восстановления доставка остановлена до завершения проверки и подтверждения.
            </p>
          )}
          {!!data.other_destination_pending && (
            <p className="warning-text small">
              Уведомлений для предыдущей конфигурации: {data.other_destination_pending}. Они не
              перенаправляются новому получателю автоматически.
            </p>
          )}
          <dl className="file-facts">
            <div>
              <dt>Ожидают / доставляются</dt>
              <dd>
                {data.counts.pending || 0} / {data.counts.in_flight || 0}
              </dd>
            </div>
            <div>
              <dt>Доставлены / отказ</dt>
              <dd>
                {data.counts.delivered || 0} / {data.counts.dead || 0}
              </dd>
            </div>
            <div>
              <dt>Зарезервировано задачами</dt>
              <dd>
                {data.reserved_jobs} · емкость {data.capacity}
              </dd>
            </div>
          </dl>
          <div className="button-row" style={{ margin: "14px 0" }}>
            <button
              className="button secondary"
              disabled={!!busy || !data.enabled}
              onClick={() => void control(data.paused ? "resume" : "pause")}
            >
              {data.paused ? <Play size={16} /> : <Pause size={16} />}
              {data.paused ? "Продолжить доставку" : "Приостановить доставку"}
            </button>
          </div>
          <p className="muted small">
            Доставка at least once: получатель проверяет HMAC, окно времени и не обрабатывает
            повторно тот же event ID. Повтор уведомления не запускает музыкальную задачу.
          </p>
          {!!data.items.length && (
            <details>
              <summary>Последние уведомления</summary>
              {data.items.map((item) => (
                <article
                  key={item.id}
                  style={{ borderTop: "1px solid var(--line)", padding: "12px 0" }}
                >
                  <div className="button-row">
                    <strong className="small">{labels[item.state] || item.state}</strong>
                    <span className="muted small">Попыток: {item.attempts}</span>
                    {item.state === "dead" && (
                      <button
                        className="button subtle"
                        disabled={!!busy || !data.enabled}
                        onClick={() => void control("retry", item.id)}
                      >
                        Повторить доставку
                      </button>
                    )}
                  </div>
                  <p className="mono break-word">{item.id}</p>
                  {item.last_code && <p className="muted small">Код: {item.last_code}</p>}
                </article>
              ))}
              <div className="button-row">
                <button
                  className="icon-button"
                  aria-label="Предыдущая страница уведомлений"
                  disabled={page === 1 || !!busy}
                  onClick={() => setPage((value) => value - 1)}
                >
                  <ChevronLeft size={18} />
                </button>
                <span className="muted small">Страница {page}</span>
                <button
                  className="icon-button"
                  aria-label="Следующая страница уведомлений"
                  disabled={data.items.length < 20 || !!busy}
                  onClick={() => setPage((value) => value + 1)}
                >
                  <ChevronRight size={18} />
                </button>
              </div>
            </details>
          )}
        </>
      )}
    </section>
  );
}
