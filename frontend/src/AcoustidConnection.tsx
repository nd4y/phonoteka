import { useEffect, useState } from "react";
import { api, messageFor } from "./api";

type Connection = {
  enabled: boolean;
  key_set: boolean;
  configured: boolean;
  transmission_allowed: boolean;
  version: string;
};

export function AcoustidConnection({
  onSaved,
  onDirtyChange,
}: {
  onSaved?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function load() {
    setNotice("");
    try {
      const value = await api<Connection>("/integrations/acoustid");
      setConnection(value);
      setEnabled(value.enabled);
      setError("");
    } catch (err) {
      setError(messageFor(err));
    }
  }
  useEffect(() => {
    void load();
  }, []);
  async function save(remove = false) {
    if (!connection) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await api<Connection>("/integrations/acoustid", {
        method: "PUT",
        body: JSON.stringify({
          version: connection.version,
          enabled: remove ? false : enabled,
          ...(remove ? { api_key: "" } : key.trim() ? { api_key: key.trim() } : {}),
        }),
      });
      setConnection(value);
      setEnabled(value.enabled);
      setKey("");
      onSaved?.();
      setNotice(
        remove
          ? "Ключ удалён, AcoustID выключен."
          : "Подключение сохранено. Можно проверить ключ. Новые задачи распознавания используют эту настройку.",
      );
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }
  async function test() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ message: string }>("/integrations/acoustid/test", {
        method: "POST",
        body: "{}",
      });
      setNotice(result.message);
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }
  const changed = !!connection && (!!key.trim() || enabled !== connection.enabled);
  useEffect(() => {
    onDirtyChange?.(changed);
  }, [changed, onDirtyChange]);
  return (
    <section className="panel settings-panel">
      <div className="panel-heading">
        <h2>AcoustID</h2>
        <button className="button subtle" disabled={busy} onClick={() => void load()}>
          Обновить подключение
        </button>
      </div>
      <p className="muted small">
        Дополнительное распознавание, если Shazam не дал однозначного результата. На сервере
        вычисляется Chromaprint; в AcoustID отправляются отпечаток и длительность. Результаты
        появятся как предложения для проверки.
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
      {!connection ? (
        <p className="muted">Загрузка подключения...</p>
      ) : (
        <>
          <p className="small">
            {connection.key_set ? "Ключ сохранён и не отображается." : "Ключ ещё не добавлен."}{" "}
            {connection.configured ? "AcoustID включён." : "AcoustID не используется."}
          </p>
          {!connection.transmission_allowed && (
            <p className="notice warning">
              Передача аудиоотпечатков запрещена конфигурацией сервера. Для работы AcoustID
              администратор должен включить EXTERNAL_FINGERPRINTS.
            </p>
          )}
          <label className="small">
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy}
              onChange={(event) => {
                setEnabled(event.target.checked);
                setNotice("");
              }}
            />{" "}
            Использовать AcoustID
          </label>
          <label style={{ display: "block", marginTop: 16 }}>
            Application API key
            <input
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              maxLength={128}
              value={key}
              disabled={busy}
              placeholder={
                connection.key_set
                  ? "Оставьте пустым, чтобы сохранить текущий ключ"
                  : "Вставьте ключ приложения AcoustID"
              }
              onChange={(event) => {
                setKey(event.target.value);
                setNotice("");
              }}
            />
          </label>
          <p className="muted small">
            Нужен ключ приложения из раздела Your Applications на acoustid.org. User API key для
            отправки отпечатков не подходит. При замене прежний ключ будет перезаписан.
          </p>
          <div className="button-row" style={{ marginTop: 16 }}>
            <button
              className="button secondary"
              disabled={busy || !changed || (enabled && !connection.key_set && !key.trim())}
              onClick={() => void save()}
            >
              Сохранить подключение
            </button>
            <button
              className="button subtle"
              disabled={busy || !connection.configured || changed}
              onClick={() => void test()}
            >
              {busy ? "Выполняется..." : "Проверить подключение"}
            </button>
            {connection.key_set && (
              <button className="button subtle" disabled={busy} onClick={() => void save(true)}>
                Удалить ключ и выключить
              </button>
            )}
          </div>
          {changed && (
            <p className="muted small">
              Сначала сохраните изменения, затем проверьте подключение. Проверка отправляет
              синтетический отпечаток и расходует один запрос суточного лимита.
            </p>
          )}
        </>
      )}
    </section>
  );
}
