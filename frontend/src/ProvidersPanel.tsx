import { useEffect, useState } from "react";
import { api, messageFor } from "./api";
import { AcoustidConnection } from "./AcoustidConnection";
type Source = {
  id: string;
  configured: boolean;
  calls_24h: number;
  retry_at?: number;
  state: string;
  capabilities: string[];
};
export function ProviderUsage({ calls, limit }: { calls: number; limit: number }) {
  return (
    <>
      {calls}
      {limit > 0
        ? ` / ${limit} запросов за 24 часа · лимит Фонотеки`
        : " запросов за 24 часа · суточная квота Фонотеки отключена"}
    </>
  );
}
export function ProvidersPanel({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const [data, setData] = useState<{ daily_limit: number; providers: Source[] } | null>(null),
    [error, setError] = useState("");
  async function load() {
    try {
      setData(await api("/providers"));
      setError("");
    } catch (e) {
      setError(messageFor(e));
    }
  }
  useEffect(() => {
    void load();
  }, []);
  const names: Record<string, string> = {
    ready: "Доступен для запросов",
    disabled: "Выключен",
    key_required: "Нужен ключ",
    rate_limited: "Лимит частоты",
    provider_unavailable: "Временно недоступен",
    provider_unauthorized: "Доступ отклонен",
  };
  return (
    <>
      <AcoustidConnection onSaved={() => void load()} onDirtyChange={onDirtyChange} />
      <section className="panel settings-panel">
        <div className="panel-heading">
          <h2>Музыкальные источники</h2>
          <button className="button secondary" onClick={() => void load()}>
            Обновить источники
          </button>
        </div>
        <p className="muted small">
          Ограничения скорости и паузы сервиса соблюдаются автоматически. Суточная квота задаётся в
          конфигурации Фонотеки и может быть отключена. AcoustID настраивается в форме подключения;
          остальные дополнительные каталоги включаются в конфигурации контейнера.
        </p>
        {error && <p role="alert">{error}</p>}
        {data?.providers.map((p) => (
          <div className="history-entry" key={p.id}>
            <strong>{p.id}</strong>
            <p className="small">
              {names[p.state] || p.state} ·{" "}
              <ProviderUsage calls={p.calls_24h} limit={data.daily_limit} />
              {p.retry_at
                ? ` · повтор после ${new Date(p.retry_at * 1000).toLocaleString("ru-RU")}`
                : ""}
            </p>
          </div>
        ))}
      </section>
    </>
  );
}
