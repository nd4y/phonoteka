import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Save } from "lucide-react";
import { api, messageFor } from "./api";
import { fieldLabels } from "./TrackPolicyPanel";
import "./FieldPolicyPanel.css";

type Priorities = {
  version: number;
  priorities: Record<string, string[]>;
  defaults: Record<string, string[]>;
  providers: string[];
  fields: string[];
  manual_first: boolean;
  automatic_apply: boolean;
};
const providerLabels: Record<string, string> = {
  acoustid: "AcoustID",
  musicbrainz: "MusicBrainz",
  shazam: "ShazamIO",
  coverartarchive: "Cover Art Archive",
  lrclib: "LRCLIB",
  local_cover: "Локальная обложка",
  local_replaygain: "Локальный ReplayGain",
  theaudiodb: "TheAudioDB",
  discogs: "Discogs",
};

export function SourcePriorityPanel() {
  const [config, setConfig] = useState<Priorities | null>(null);
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [field, setField] = useState("title");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const dirty = !!config && JSON.stringify(draft) !== JSON.stringify(config.priorities);
  const order = draft[field] || config?.defaults[field] || [];
  useEffect(() => {
    let active = true;
    setError("");
    void api<Priorities>("/policies/sources")
      .then((result) => {
        if (active) {
          setConfig(result);
          setDraft(result.priorities);
        }
      })
      .catch((err) => {
        if (active) setError(messageFor(err));
      });
    return () => {
      active = false;
    };
  }, [reload]);
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  function move(index: number, direction: -1 | 1) {
    const result = [...order];
    [result[index], result[index + direction]] = [result[index + direction], result[index]];
    setDraft((current) => ({ ...current, [field]: result }));
    setNotice("");
  }
  async function save() {
    if (!config) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<Priorities>("/policies/sources", {
        method: "PUT",
        body: JSON.stringify({ priorities: draft, base_version: config.version }),
      });
      setConfig(result);
      setDraft(result.priorities);
      setNotice("Приоритеты сохранены. Новые предложения используют этот порядок.");
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel settings-panel settings-wide source-priority-panel">
      <h2>Приоритеты источников</h2>
      <p className="muted small">
        Закрепленные вручную значения всегда первыми. Если вы выбираете несколько источников,
        приоритет определяется отдельно для каждого поля. Результат остается предложением для
        проверки; автоматическая запись выключена. Изменение порядка не включает отключенный сервис.
      </p>
      {error && (
        <p role="alert" className="warning-text">
          {error}
        </p>
      )}
      {!config ? (
        <button className="button subtle" onClick={() => setReload((value) => value + 1)}>
          {error ? "Повторить загрузку" : "Загрузка приоритетов..."}
        </button>
      ) : (
        <>
          <label>
            Поле
            <select
              value={field}
              disabled={busy}
              onChange={(event) => setField(event.target.value)}
            >
              {config.fields.map((value) => (
                <option value={value} key={value}>
                  {fieldLabels[value] || value}
                </option>
              ))}
            </select>
          </label>
          <ol className="source-priority-list">
            {order.map((provider, index) => (
              <li key={provider}>
                <span>
                  <strong>{providerLabels[provider] || provider}</strong>
                  <span className="muted small">Приоритет {index + 1}</span>
                </span>
                <div className="button-row">
                  <button
                    className="icon-button"
                    disabled={busy || index === 0}
                    aria-label={`Повысить приоритет ${providerLabels[provider] || provider}`}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp size={17} />
                  </button>
                  <button
                    className="icon-button"
                    disabled={busy || index === order.length - 1}
                    aria-label={`Понизить приоритет ${providerLabels[provider] || provider}`}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown size={17} />
                  </button>
                </div>
              </li>
            ))}
          </ol>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={busy || !dirty}
              onClick={() => void save()}
            >
              <Save size={16} />
              {busy ? "Сохранение..." : "Сохранить приоритеты"}
            </button>
            <button
              className="button subtle"
              disabled={busy || !draft[field]}
              onClick={() =>
                setDraft((current) => {
                  const result = { ...current };
                  delete result[field];
                  return result;
                })
              }
            >
              Вернуть порядок поля по умолчанию
            </button>
          </div>
          <p className="muted small">
            Версия {config.version}
            {dirty ? " · есть несохраненные изменения" : ""}
          </p>
        </>
      )}
      {notice && (
        <p className="muted small" role="status">
          {notice}
        </p>
      )}
    </section>
  );
}
