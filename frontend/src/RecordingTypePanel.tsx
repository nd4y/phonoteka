import { useEffect, useRef, useState } from "react";
import { api, messageFor } from "./api";
import type { RecordingKind, Track } from "./types";

export function recordingLabel(track: Track) {
  if (track.possible_mix || track.record_type === "possible_mix") return "Возможный микс";
  return track.record_type === "single_recording" ? "Отдельная запись" : "";
}

export function recordingPanelKey(track: Track) {
  const current =
    track.recording_classification?.input_sha256 === track.sha256
      ? track.recording_classification
      : undefined;
  return JSON.stringify([
    track.id,
    track.sha256,
    !!track.possible_mix,
    track.record_type,
    current?.kind,
    current?.origin,
    current?.version,
  ]);
}

export function classificationRequest(track: Track, kind: RecordingKind, reason: string) {
  const current =
    track.recording_classification?.input_sha256 === track.sha256
      ? track.recording_classification
      : undefined;
  return {
    method: "PUT",
    headers: { "If-Match": track.sha256 },
    body: JSON.stringify({ kind, reason: reason.trim(), base_version: current?.version ?? 0 }),
  };
}

export function RecordingTypeForm({
  kind,
  reason,
  disabled,
  saving,
  onKind,
  onReason,
  onSave,
}: {
  kind: RecordingKind;
  reason: string;
  disabled: boolean;
  saving: boolean;
  onKind: (kind: RecordingKind) => void;
  onReason: (reason: string) => void;
  onSave: () => void;
}) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <label>
        Тип записи
        <select
          aria-label="Тип записи"
          value={kind}
          disabled={disabled || saving}
          onChange={(event) => onKind(event.target.value as RecordingKind)}
        >
          <option value="single_recording">Отдельная запись</option>
          <option value="possible_mix">Возможный микс или сборная запись</option>
        </select>
      </label>
      <label>
        Основание решения
        <textarea
          aria-label="Основание решения о типе записи"
          value={reason}
          minLength={5}
          maxLength={1000}
          disabled={disabled || saving}
          onChange={(event) => onReason(event.target.value)}
        />
      </label>
      <button
        className="button secondary"
        type="submit"
        disabled={disabled || saving || reason.trim().length < 5}
      >
        {saving ? "Сохранение..." : "Подтвердить тип записи"}
      </button>
    </form>
  );
}

export function RecordingTypePanel({
  track,
  disabled,
  onDirtyChange,
  onChanged,
}: {
  track: Track;
  disabled: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onChanged: (track: Track) => void;
}) {
  const initialKind: RecordingKind = track.possible_mix ? "possible_mix" : "single_recording";
  const [kind, setKind] = useState<RecordingKind>(initialKind),
    [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false),
    [error, setError] = useState("");
  const serial = useRef(0);
  const cue = track.format === "cue" || !!track.cue_album_image;
  const dirty = !cue && (reason.length > 0 || kind !== initialKind);
  useEffect(() => {
    onDirtyChange(dirty || saving);
  }, [dirty, saving, onDirtyChange]);
  useEffect(
    () => () => {
      serial.current += 1;
    },
    [track.id, track.sha256],
  );
  const current =
    track.recording_classification?.input_sha256 === track.sha256
      ? track.recording_classification
      : undefined;
  async function save() {
    if (disabled || saving || cue || reason.trim().length < 5) return;
    const request = serial.current;
    setSaving(true);
    setError("");
    try {
      await api(
        `/tracks/${encodeURIComponent(track.id)}/recording-classification`,
        classificationRequest(track, kind, reason),
      );
      const refreshed = await api<Track>(`/tracks/${encodeURIComponent(track.id)}`);
      if (request !== serial.current) return;
      setReason("");
      onDirtyChange(false);
      onChanged(refreshed);
    } catch (problem) {
      if (request === serial.current) setError(messageFor(problem));
    } finally {
      if (request === serial.current) setSaving(false);
    }
  }
  return (
    <section className="panel settings-panel" aria-labelledby={`recording-type-${track.id}`}>
      <h2 id={`recording-type-${track.id}`}>Состав записи</h2>
      {track.possible_mix ? (
        <p className="warning-text" role="status">
          Возможно, файл содержит несколько записей. Теги отдельной песни, автоматическая запись и
          публикация заблокированы до подтверждения.
        </p>
      ) : (
        <p className="muted small">
          {recordingLabel(track) || "Тип записи пока не подтверждён вручную."}
        </p>
      )}
      {current && (
        <p className="muted small">
          {current.origin === "owner"
            ? `Подтверждено вручную, версия ${current.version}: ${current.reason}`
            : "Разные фрагменты аудио сопоставлены с разными записями."}
        </p>
      )}
      <p className="muted small">
        Подтверждение относится к текущей ревизии файла. Оно не принимает теги, текст или таймкоды и
        не разделяет аудио на композиции.
      </p>
      {cue ? (
        <p className="muted small">Тип CUE и альбомного образа определяется документом CUE.</p>
      ) : (
        <RecordingTypeForm
          kind={kind}
          reason={reason}
          disabled={disabled}
          saving={saving}
          onKind={setKind}
          onReason={setReason}
          onSave={() => void save()}
        />
      )}
      {error && (
        <p className="warning-text" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
