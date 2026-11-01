import { useEffect, useState } from "react";
import {
  CircleAlert,
  CheckCheck,
  Headphones,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
} from "lucide-react";
import { api, messageFor } from "./api";
import { formatTime } from "./lyrics";
import type { Track } from "./types";
import "./LyricsReviewPanel.css";

type Decision = "unknown" | "accepted" | "uncertain";
type Profile = "lyrics_required" | "instrumental" | "relaxed";
type Section = "start" | "middle" | "end";
interface ReviewLine {
  line_id: string;
  ordinal: number;
  text: string;
  start_ms: number | null;
  timing_valid: boolean;
  content: boolean;
  words_state: Decision;
  timing_state: Decision;
}
interface ReviewData {
  version: number;
  file_revision: string;
  document_revision: string;
  words_revision: string;
  timings_revision: string;
  lines: ReviewLine[];
  listened_sections: Section[];
  profile: Profile;
  reason: string;
  instrumental_confirmed: boolean;
  stale: boolean;
  accepted_current_revision: boolean;
  human_timing_verified: boolean;
  gate: { state: string; reasons: string[]; exceptions: string[]; exception_reason: string };
}
interface Props {
  track: Track;
  disabled?: boolean;
  onSeek?: (seconds: number) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onChanged?: () => void;
}
const reasonNames: Record<string, string> = {
  critical_file_conflict: "Критический конфликт или ошибка файла",
  provider_export_denied: "Источник не разрешает экспорт данных",
  current_decode_audit_required: "Нужен актуальный успешный аудит декодирования",
  accepted_file_revision_required: "Ревизия файла ещё не принята вручную",
  decode_audit_failed: "Декодирование текущего файла завершилось ошибкой или формат не поддержан",
  missing_title: "Не указано название",
  missing_artist: "Не указан исполнитель",
  missing_album: "Не указан альбом",
  missing_cover: "Нет обложки",
  lyrics_required: "Текст не найден",
  complete_synced_lyrics_required: "Нужны допустимые таймкоды всех строк",
  line_listening_review_required: "Не все содержательные строки проверены по аудио",
  start_middle_end_listening_required: "Не проверены начало, середина и конец",
  owner_instrumental_confirmation_required: "Инструментальность требует явного подтверждения",
  instrumental_has_lyrics_conflict: "У инструментала есть содержательный текст: устраните конфликт",
  owner_exception_reason_required: "Укажите причину исключения",
  invalid_lyrics: "Некорректный LRC",
  invalid_timing: "Недопустимый порядок или диапазон времён",
};
const sectionNames: Record<Section, string> = { start: "Начало", middle: "Середина", end: "Конец" };

export default function LyricsReviewPanel({
  track,
  disabled = false,
  onSeek,
  onDirtyChange,
  onChanged,
}: Props) {
  const [review, setReview] = useState<ReviewData | null>(null);
  const [lines, setLines] = useState<ReviewLine[]>([]);
  const [sections, setSections] = useState<Section[]>([]);
  const [profile, setProfile] = useState<Profile>("lyrics_required");
  const [reason, setReason] = useState("");
  const [instrumental, setInstrumental] = useState(false);
  const [loading, setLoading] = useState(true),
    [saving, setSaving] = useState(false),
    [dirty, setDirty] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [generation, setGeneration] = useState(0);
  function receive(data: ReviewData) {
    setReview(data);
    setLines(data.lines);
    setSections(data.listened_sections);
    setProfile(data.profile);
    setReason(data.reason);
    setInstrumental(data.instrumental_confirmed);
    setDirty(false);
  }
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setNotice("");
    setReview(null);
    setLines([]);
    setDirty(false);
    api<ReviewData>(`/tracks/${encodeURIComponent(track.id)}/lyrics-review`)
      .then((data) => {
        if (active) receive(data);
      })
      .catch((err) => {
        if (active) setError(messageFor(err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [track.id, track.sha256, generation]);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  const blocked = disabled || loading || saving;
  function changeLine(identity: string, field: "words_state" | "timing_state", value: Decision) {
    setLines((old) =>
      old.map((line) => (line.line_id === identity ? { ...line, [field]: value } : line)),
    );
    setDirty(true);
    setNotice("");
  }
  async function save(acceptRevision = false) {
    if (!review || blocked) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const data = await api<ReviewData>(`/tracks/${encodeURIComponent(track.id)}/lyrics-review`, {
        method: "PUT",
        headers: { "If-Match": `"${review.file_revision}"` },
        body: JSON.stringify({
          base_version: review.version,
          document_revision: review.document_revision,
          profile,
          reason,
          instrumental_confirmed: instrumental,
          lines: lines.map(({ line_id, words_state, timing_state }) => ({
            line_id,
            words_state,
            timing_state,
          })),
          listened_sections: sections,
          accept_revision: acceptRevision,
        }),
      });
      receive(data);
      setNotice(
        acceptRevision
          ? "Решение о текущей ревизии сохранено. Готовность к публикации показана отдельно."
          : "Построчная проверка сохранена. Аудиофайл не изменён.",
      );
      onChanged?.();
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setSaving(false);
    }
  }
  const reviewed = lines.filter(
    (line) => line.content && line.words_state === "accepted" && line.timing_state === "accepted",
  ).length;
  const contentCount = lines.filter((line) => line.content).length;
  return (
    <section
      className="lyrics-review-panel"
      aria-label="Ручная проверка текста и профиля публикации"
    >
      <div className="lyrics-review-heading">
        <Headphones size={20} />
        <div>
          <h3>Проверка по аудио и публикация</h3>
          <p>
            Принятые слова, времена и ревизия файла хранятся раздельно. Наличие таймкодов само не
            подтверждает синхронизацию.
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Обновить построчную проверку"
          disabled={blocked || dirty}
          onClick={() => setGeneration((value) => value + 1)}
        >
          <RefreshCw size={17} className={loading ? "spin" : ""} />
        </button>
      </div>
      {loading && (
        <p className="muted small" role="status">
          Загружаем проверку текущей ревизии…
        </p>
      )}
      {error && (
        <div className="notice error" role="alert">
          <CircleAlert size={17} />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="notice success" role="status">
          <ShieldCheck size={17} />
          <span>{notice}</span>
        </div>
      )}
      {review && !loading && (
        <>
          {review.stale && (
            <p className="notice warning">
              <CircleAlert size={17} />
              <span>
                Аудио, слова или времена изменились. Старые отметки сохранены в истории и не
                подтверждают эту ревизию. Проверьте строки заново.
              </span>
            </p>
          )}
          <div className="lyrics-review-state">
            <span className={`badge ${review.human_timing_verified ? "good" : "warm"}`}>
              {review.human_timing_verified
                ? "Таймкоды проверены вручную"
                : `Построчно проверено ${reviewed} из ${contentCount}`}
            </span>
            <span className={`badge ${review.accepted_current_revision ? "good" : "purple"}`}>
              {review.accepted_current_revision
                ? "Ревизия принята вручную"
                : "Ревизия ещё не принята"}
            </span>
            <span className="badge">Poweramp: отдельная проверка доставки</span>
          </div>
          <p className="muted small">
            Для каждой содержательной строки прослушайте начало вокала, проверьте слова и время.
            Сомнительные участки отмечайте «Нужна проверка». Автоматического принятия ASR нет.
          </p>
          {!!lines.length && (
            <div
              className="lyrics-review-table-scroll"
              tabIndex={0}
              aria-label="Построчное принятие слов и времени"
            >
              <table>
                <thead>
                  <tr>
                    <th scope="col">Строка и время</th>
                    <th scope="col">Слова</th>
                    <th scope="col">Время</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line) => (
                    <tr key={line.line_id}>
                      <td>
                        <button
                          className="timing-time"
                          disabled={blocked || !onSeek || !line.timing_valid}
                          onClick={() => onSeek?.(line.start_ms! / 1000)}
                          aria-label={`Прослушать строку ${line.ordinal + 1}`}
                        >
                          <Play size={11} />
                          {formatTime(line.start_ms) || "Нет времени"}
                        </button>
                        <span className="lyrics-review-line">
                          {line.ordinal + 1}. {line.text || "Пустая временная строка"}
                        </span>
                      </td>
                      <td>
                        <select
                          aria-label={`Слова строки ${line.ordinal + 1}`}
                          value={line.words_state}
                          disabled={blocked}
                          onChange={(event) =>
                            changeLine(line.line_id, "words_state", event.target.value as Decision)
                          }
                        >
                          <option value="unknown">Не проверены</option>
                          <option value="accepted">Проверены по аудио</option>
                          <option value="uncertain">Нужна проверка</option>
                        </select>
                      </td>
                      <td>
                        <select
                          aria-label={`Время строки ${line.ordinal + 1}`}
                          value={line.timing_state}
                          disabled={blocked}
                          onChange={(event) =>
                            changeLine(line.line_id, "timing_state", event.target.value as Decision)
                          }
                        >
                          <option value="unknown">Не проверено</option>
                          <option value="accepted" disabled={!line.timing_valid}>
                            Проверено по вокалу
                          </option>
                          <option value="uncertain">Нужна проверка</option>
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!lines.length && (
            <p className="quiet-note">
              В текущем файле нет строк текста для проверки. Отсутствие ASR-слов не доказывает
              инструментальность.
            </p>
          )}
          <fieldset className="lyrics-review-sections" disabled={blocked}>
            <legend>Участки, прослушанные вручную</legend>
            {(["start", "middle", "end"] as Section[]).map((section) => (
              <label key={section}>
                <input
                  type="checkbox"
                  checked={sections.includes(section)}
                  onChange={(event) => {
                    setSections((old) =>
                      event.target.checked
                        ? [...old, section]
                        : old.filter((value) => value !== section),
                    );
                    setDirty(true);
                  }}
                />
                {sectionNames[section]}
              </label>
            ))}
          </fieldset>
          <div className="lyrics-review-profile">
            <label>
              Профиль публикации
              <select
                value={profile}
                disabled={blocked}
                onChange={(event) => {
                  setProfile(event.target.value as Profile);
                  setDirty(true);
                }}
              >
                <option value="lyrics_required">Слова и все таймкоды проверены</option>
                <option value="instrumental">Подтверждённый инструментал</option>
                <option value="relaxed">Личное исключение с причиной</option>
              </select>
            </label>
            {profile === "instrumental" && (
              <label className="lyrics-instrumental">
                <input
                  type="checkbox"
                  checked={instrumental}
                  disabled={blocked}
                  onChange={(event) => {
                    setInstrumental(event.target.checked);
                    setDirty(true);
                  }}
                />
                Я прослушал запись и подтверждаю отсутствие вокального текста
              </label>
            )}
            {profile !== "lyrics_required" && (
              <label>
                Причина исключения
                <textarea
                  value={reason}
                  maxLength={1000}
                  rows={2}
                  disabled={blocked}
                  onChange={(event) => {
                    setReason(event.target.value);
                    setDirty(true);
                  }}
                  placeholder="Что проверено и почему публикация допустима без обычных требований к тексту/обложке"
                />
              </label>
            )}
          </div>
          <div className={`lyrics-publication-gate ${review.gate.state}`}>
            <ShieldCheck size={18} />
            <div>
              <strong>
                {review.gate.state === "passed"
                  ? "Текущая сохранённая ревизия допускается профилем"
                  : review.gate.state === "blocked"
                    ? "Публикация заблокирована"
                    : "До публикации нужна проверка"}
              </strong>
              {review.gate.reasons.length > 0 && (
                <ul>
                  {review.gate.reasons.map((code) => (
                    <li key={code}>{reasonNames[code] || code}</li>
                  ))}
                </ul>
              )}
              {!!review.gate.exceptions.length && (
                <p>
                  Исключения сохранятся в manifest:{" "}
                  {review.gate.exception_reason || "подтверждённый инструментал"}.
                </p>
              )}
            </div>
          </div>
          {dirty && (
            <p className="warning-text small">
              Изменения проверки ещё не сохранены. Показанный допуск относится к последнему
              сохранённому решению.
            </p>
          )}
          <div className="button-row lyrics-review-actions">
            <button
              className="button secondary"
              disabled={blocked || !dirty}
              onClick={() => void save()}
            >
              <Save size={16} />
              Сохранить проверку строк
            </button>
            <button
              className="button secondary"
              disabled={
                blocked ||
                (profile !== "lyrics_required" && !reason.trim()) ||
                (profile === "instrumental" && !instrumental)
              }
              onClick={() => void save(true)}
            >
              <CheckCheck size={16} />
              Принять текущую ревизию
            </button>
          </div>
          <p className="muted small">
            Эти действия сохраняют подтверждение. Запись тегов, публикация и проверка в Poweramp
            выполняются отдельно. Ошибку декодирования или запрет источника нельзя отменить профилем
            исключения.
          </p>
        </>
      )}
    </section>
  );
}
