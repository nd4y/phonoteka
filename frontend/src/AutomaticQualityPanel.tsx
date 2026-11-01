import { useEffect, useRef, useState } from "react";
import { api, messageFor } from "./api";

type Decision = {
  version: number;
  recording_candidate_id?: string;
  fingerprint_candidate_id?: string;
  performance_version?: string;
  edition_mode?: string;
  release_review_id?: string | null;
  release_review_version?: number | null;
  language?: string;
  reason?: string;
};
type Result = {
  state: string;
  reason_codes: string[];
  dependency_sha256: string;
  file_revision: string;
  profile: { id: string; version: number; enabled: boolean; threshold_status: string };
  evidence: { content_lines?: number; reliable_lines?: number };
  identity_review: Decision;
  identity_options: Array<{
    id: string;
    provider: string;
    title: string;
    artist: string;
    kind: string;
  }>;
};
const reasons: Record<string, string> = {
  automatic_apply_disabled: "Автоприменение выключено",
  pilot_not_validated: "Музыкальный приемочный пилот еще не подтвержден",
  authorization_write_required: "Нет полномочий на автоматическую запись",
  storage_write_disabled: "Запись в коллекцию выключена",
  file_revision_stale: "Файл изменился вне приложения",
  critical_file_conflict: "Есть критический конфликт файла",
  format_not_writable: "Формат открыт только для чтения",
  current_audio_audit_required: "Нужен актуальный аудит аудио",
  proposal_stale_or_missing: "Нет текущего сохраненного предложения",
  current_analysis_required: "Нужен актуальный анализ",
  source_priority_stale: "Приоритеты источников изменились",
  provider_usage_denied: "Политика источника запрещает использование данных",
  proposal_not_server_candidate: "Измененные значения не подтверждены сохраненным источником",
  recording_identity_unverified: "Не подтверждена конкретная запись",
  performance_version_unverified: "Не подтверждена версия исполнения",
  recording_duration_conflict_or_unknown: "Длительность не подтверждает выбранную запись",
  analysis_conflict_unresolved: "Не разрешен конфликт результатов анализа",
  possible_mix_requires_manual_review: "Возможный микс нескольких записей требует ручной проверки",
  alignment_result_malformed: "Неверная структура результата выравнивания",
  recording_only_album_write: "Режим только записи запрещает придумывать альбом",
  release_selection_unverified: "Не подтвержден выбранный релиз",
  release_recording_conflict: "Запись не совпадает с позицией релиза",
  explicit_edition_mode_required: "Выберите конкретный релиз или режим только записи",
  cover_release_unverified: "Обложка не привязана к подтвержденному релизу",
  previously_accepted_words_required: "Сначала должны быть приняты все исходные слова",
  alignment_dependency_stale: "Выравнивание относится к другой версии слов",
  language_unsupported_or_unconfirmed: "Язык не поддержан или не подтверждён",
  aligner_profile_unsupported: "Метод выравнивания не включен в профиль",
  independent_asr_provenance_missing:
    "Нет независимой ASR с серверным подтверждением происхождения",
  independent_asr_contradiction: "Независимая ASR противоречит словам",
  timing_invalid_or_not_proposed: "Времена неверны или не совпадают с предложением",
  unexplained_timing_gap: "Есть необъясненный большой разрыв",
  line_quality_below_profile: "Одна из строк не прошла пороги качества",
  complete_reliable_timings_required: "Надежные времена нужны для 100% содержательных строк",
};

export function AutomaticQualityPanel({
  trackId,
  revision,
  proposalId,
  changed,
}: {
  trackId: string;
  revision: string;
  proposalId?: string;
  changed: boolean;
}) {
  const [result, setResult] = useState<Result | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [decision, setDecision] = useState<Decision>({
    version: 0,
    performance_version: "",
    edition_mode: "",
    language: "",
    reason: "",
  });
  const serial = useRef(0);
  useEffect(() => {
    serial.current++;
    setResult(null);
    setError("");
    setBusy(false);
  }, [trackId, revision, proposalId]);
  async function preview() {
    const current = ++serial.current;
    setBusy(true);
    setError("");
    try {
      const value = await api<Result>(
        `/tracks/${encodeURIComponent(trackId)}/automatic-quality${proposalId ? "?proposal_id=" + encodeURIComponent(proposalId) : ""}`,
      );
      if (serial.current === current) {
        setResult(value);
        setDecision({
          performance_version: "",
          edition_mode: "",
          language: "",
          reason: "",
          ...value.identity_review,
        });
      }
    } catch (err) {
      if (serial.current === current) setError(messageFor(err));
    } finally {
      if (serial.current === current) setBusy(false);
    }
  }
  async function saveIdentity() {
    if (!result) return;
    setBusy(true);
    setError("");
    const current = ++serial.current;
    try {
      await api(`/tracks/${encodeURIComponent(trackId)}/automatic-quality/identity`, {
        method: "PUT",
        headers: { "If-Match": `"${revision}"` },
        body: JSON.stringify({
          base_version: decision.version,
          recording_candidate_id: decision.recording_candidate_id,
          fingerprint_candidate_id: decision.fingerprint_candidate_id,
          performance_version: decision.performance_version,
          edition_mode: decision.edition_mode,
          language: decision.language,
          reason: decision.reason,
          release_review_id:
            decision.edition_mode === "release" ? decision.release_review_id : null,
          release_review_version:
            decision.edition_mode === "release" ? decision.release_review_version : null,
        }),
      });
      if (serial.current === current) await preview();
    } catch (err) {
      if (serial.current === current) setError(messageFor(err));
    } finally {
      if (serial.current === current) setBusy(false);
    }
  }
  return (
    <section className="automatic-quality-panel">
      <h3>Проверка автоматического принятия</h3>
      <p className="muted small">
        Проверяет серверные зависимости и каждую содержательную строку. Музыкальный пилот еще не
        принят, поэтому автоприменение выключено. Ручная проверка и сохранение предложения доступны
        отдельно.
      </p>
      <button
        className="button secondary"
        disabled={busy || changed}
        onClick={() => void preview()}
      >
        Показать причины и зависимости
      </button>
      {changed && (
        <p className="muted small">
          Сначала сохраните черновик, чтобы проверить конкретное предложение.
        </p>
      )}
      {error && (
        <p role="alert" className="warning-text">
          {error}
        </p>
      )}
      {result && (
        <>
          <p role="status">
            {result.state === "passed"
              ? "Проверка пройдена"
              : result.state === "blocked"
                ? "Автоматическое применение заблокировано"
                : "Нужна ручная проверка"}{" "}
            · {result.profile.id} v{result.profile.version}
          </p>
          <ul>
            {result.reason_codes.map((code) => (
              <li key={code}>{reasons[code] || reasons[code.split(":")[0]] || code}</li>
            ))}
          </ul>
          {result.evidence.content_lines !== undefined && (
            <p className="small">
              Строк с надежным временем: {result.evidence.reliable_lines}/
              {result.evidence.content_lines}
            </p>
          )}
          <p className="muted small">
            Snapshot зависимостей:{" "}
            <code title={result.dependency_sha256}>{result.dependency_sha256.slice(0, 16)}…</code>
          </p>
          <details>
            <summary>Подтверждение конкретной записи</summary>
            <p className="muted small">
              Выберите реальные сохраненные результаты. Это ручное основание выбора, оно не
              подтверждает музыкальный пилот и не включает автозапись.
            </p>
            <label>
              MusicBrainz recording
              <select
                value={decision.recording_candidate_id || ""}
                disabled={busy}
                onChange={(event) =>
                  setDecision({ ...decision, recording_candidate_id: event.target.value })
                }
              >
                <option value="">Выберите запись</option>
                {result.identity_options
                  .filter((item) => item.kind === "recording")
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.artist} · {item.title}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Совпадение по аудиоотпечатку
              <select
                value={decision.fingerprint_candidate_id || ""}
                disabled={busy}
                onChange={(event) =>
                  setDecision({ ...decision, fingerprint_candidate_id: event.target.value })
                }
              >
                <option value="">Выберите совпадение</option>
                {result.identity_options
                  .filter((item) => item.kind === "fingerprint")
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.provider} · {item.artist} · {item.title}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Версия исполнения
              <select
                value={decision.performance_version}
                disabled={busy}
                onChange={(event) =>
                  setDecision({ ...decision, performance_version: event.target.value })
                }
              >
                <option value="">Выберите подтвержденную версию исполнения</option>
                <option value="studio">Студийная</option>
                <option value="live">Концертная</option>
                <option value="edit">Редакция / edit</option>
                <option value="remaster">Ремастер</option>
                <option value="other_owner_reviewed">Другая, проверена вручную</option>
              </select>
            </label>
            <label>
              Издание
              <select
                value={decision.edition_mode}
                disabled={busy}
                onChange={(event) => setDecision({ ...decision, edition_mode: event.target.value })}
              >
                <option value="">Выберите подтвержденный режим издания</option>
                <option value="recording_only">
                  Только запись, без изменения альбомных данных
                </option>
                <option value="release">Конкретный сверенный релиз</option>
              </select>
            </label>
            {decision.edition_mode === "release" && (
              <>
                <label>
                  ID сохраненной сверки релиза
                  <input
                    value={decision.release_review_id || ""}
                    disabled={busy}
                    maxLength={36}
                    onChange={(event) =>
                      setDecision({ ...decision, release_review_id: event.target.value })
                    }
                  />
                </label>
                <label>
                  Версия сверки
                  <input
                    type="number"
                    min={1}
                    value={decision.release_review_version || ""}
                    disabled={busy}
                    onChange={(event) =>
                      setDecision({
                        ...decision,
                        release_review_version: Number(event.target.value) || null,
                      })
                    }
                  />
                </label>
              </>
            )}
            <label>
              Подтвержденный язык
              <select
                value={decision.language}
                disabled={busy}
                onChange={(event) => setDecision({ ...decision, language: event.target.value })}
              >
                <option value="">Выберите подтвержденный язык</option>
                <option value="en">Английский</option>
                <option value="ru">Русский</option>
              </select>
            </label>
            <label>
              Основание проверки
              <input
                value={decision.reason || ""}
                maxLength={1000}
                disabled={busy}
                onChange={(event) => setDecision({ ...decision, reason: event.target.value })}
              />
            </label>
            <button
              className="button secondary"
              disabled={
                busy ||
                !decision.recording_candidate_id ||
                !decision.fingerprint_candidate_id ||
                !decision.performance_version ||
                !decision.edition_mode ||
                !decision.language ||
                (decision.reason?.trim().length || 0) < 5 ||
                (decision.edition_mode === "release" &&
                  (!decision.release_review_id || !decision.release_review_version))
              }
              onClick={() => void saveIdentity()}
            >
              Сохранить ручное основание выбора
            </button>
          </details>
        </>
      )}
    </section>
  );
}
