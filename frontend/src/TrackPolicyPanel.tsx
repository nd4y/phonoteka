import { useEffect, useRef, useState } from "react";
import { LockKeyhole, RefreshCw, Save } from "lucide-react";
import { api, messageFor } from "./api";
import type { Track } from "./types";
import "./FieldPolicyPanel.css";

export const fieldLabels: Record<string, string> = {
  title: "Название",
  artist: "Исполнитель",
  albumartist: "Исполнитель альбома",
  album: "Альбом",
  tracknumber: "Номер трека",
  discnumber: "Номер диска",
  date: "Дата релиза",
  originaldate: "Дата первого издания",
  genre: "Жанр",
  composer: "Композитор",
  lyricist: "Автор текста",
  cover: "Обложка",
  lrc: "Текст и таймкоды",
  replaygain_track_gain: "Track Gain",
  replaygain_track_peak: "Track Peak",
  replaygain_album_gain: "Album Gain",
  replaygain_album_peak: "Album Peak",
  musicbrainz_trackid: "MusicBrainz Recording ID",
  musicbrainz_albumid: "MusicBrainz Release ID",
  musicbrainz_artistid: "MusicBrainz Artist ID",
  musicbrainz_albumartistid: "MusicBrainz Album Artist ID",
  musicbrainz_releasetrackid: "MusicBrainz Release Track ID",
};
type TrackPolicy = {
  version: number;
  input_sha256: string;
  fields: string[];
  stale_fields: string[];
  locks: Record<string, { input_sha256: string; created_at: string }>;
};

export function TrackPolicyPanel({
  track,
  disabled = false,
  onChanged,
  onDirtyChange,
}: {
  track: Track;
  disabled?: boolean;
  onChanged?: () => Promise<void> | void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [policy, setPolicy] = useState<TrackPolicy | null>(null);
  const [fields, setFields] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const requestSerial = useRef(0);
  const dirty =
    !!policy &&
    JSON.stringify([...fields].sort()) !== JSON.stringify(Object.keys(policy.locks).sort());
  useEffect(() => {
    let active = true;
    requestSerial.current += 1;
    setPolicy(null);
    setError("");
    setNotice("");
    setBusy(false);
    void api<TrackPolicy>(`/tracks/${encodeURIComponent(track.id)}/policy`)
      .then((result) => {
        if (active) {
          setPolicy(result);
          setFields(Object.keys(result.locks));
        }
      })
      .catch((err) => {
        if (active) setError(messageFor(err));
      });
    return () => {
      active = false;
      requestSerial.current += 1;
    };
  }, [track.id, track.sha256, reload]);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  async function save() {
    if (!policy) return;
    const serial = requestSerial.current;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<TrackPolicy>(`/tracks/${encodeURIComponent(track.id)}/policy`, {
        method: "PUT",
        headers: { "If-Match": policy.input_sha256 },
        body: JSON.stringify({ fields, base_version: policy.version }),
      });
      if (serial !== requestSerial.current) return;
      setPolicy(result);
      setFields(Object.keys(result.locks));
      setNotice("Закрепления сохранены. Последующий поиск не заменит эти значения.");
      await onChanged?.();
    } catch (err) {
      if (serial === requestSerial.current) setError(messageFor(err));
    } finally {
      if (serial === requestSerial.current) setBusy(false);
    }
  }
  function valueOf(field: string) {
    if (field === "cover")
      return track.has_art ? "Текущая встроенная обложка" : "Обложка отсутствует";
    if (field === "lrc")
      return track.lrc
        ? "Текущий синхронизированный текст целиком"
        : track.lyrics
          ? "Текущий обычный текст целиком"
          : "Текст отсутствует";
    const value = track.tags[field];
    return Array.isArray(value)
      ? value.join("; ")
      : value === undefined || value === null || value === ""
        ? "Поле пустое"
        : String(value);
  }
  return (
    <section className="field-policy-panel" aria-labelledby={`field-policy-${track.id}`}>
      <div className="section-intro">
        <div>
          <h3 id={`field-policy-${track.id}`}>
            <LockKeyhole size={18} /> Закрепленные поля
          </h3>
          <p className="muted small">
            Закрепляется значение, уже встроенное в файл. Сначала примените свои правки, затем
            закрепите их. Чтобы заменить защищенное поле, снимите закрепление.
          </p>
        </div>
      </div>
      {error && (
        <p className="warning-text" role="alert">
          {error}
        </p>
      )}
      {policy?.stale_fields.length ? (
        <p className="warning-text" role="alert">
          Значения изменились после закрепления:{" "}
          {policy.stale_fields.map((field) => fieldLabels[field] || field).join(", ")}. Запись
          заблокирована до подтверждения. Снимите устаревшие закрепления и сохраните решение;
          повторное закрепление фиксирует уже проверенное текущее значение.
        </p>
      ) : null}
      {!policy ? (
        <button
          className="button subtle"
          disabled={busy}
          onClick={() => setReload((value) => value + 1)}
        >
          <RefreshCw size={16} />
          {error ? "Повторить загрузку" : "Загрузка закреплений..."}
        </button>
      ) : (
        <>
          <div className="field-policy-grid">
            {policy.fields.map((field) => (
              <label
                className={`field-policy-choice ${fields.includes(field) ? "is-locked" : ""}`}
                key={field}
              >
                <input
                  type="checkbox"
                  checked={fields.includes(field)}
                  disabled={disabled || busy || policy.input_sha256 !== track.sha256}
                  onChange={(event) =>
                    setFields((current) =>
                      event.target.checked
                        ? [...current, field]
                        : current.filter((value) => value !== field),
                    )
                  }
                />
                <span>
                  <strong>{fieldLabels[field] || field}</strong>
                  <span className="muted small" title={valueOf(field)}>
                    {valueOf(field)}
                  </span>
                </span>
              </label>
            ))}
          </div>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={disabled || busy || !dirty}
              onClick={() => void save()}
            >
              <Save size={16} />
              {busy ? "Сохранение..." : "Сохранить закрепления"}
            </button>
            <span className="muted small">
              {Object.keys(policy.locks).length} закреплено · версия {policy.version}
              {dirty ? " · есть несохраненные изменения" : ""}
            </span>
          </div>
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
