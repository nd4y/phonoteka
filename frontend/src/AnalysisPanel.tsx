import { Search, RefreshCw, CircleAlert } from "lucide-react";
import { useState } from "react";
import type { Track } from "./types";
import { SourceCandidatePicker } from "./SourceCandidatePicker";
import { AutomaticQualityPanel } from "./AutomaticQualityPanel";
import { alignmentStatus, instrumentalTextConflict } from "./alignmentStatus";
const states: Record<string, string> = {
  fingerprint_match: "Совпадение по отпечатку",
  candidates: "Найдены варианты",
  candidate: "Есть вариант",
  no_match: "Не найдено",
  key_required: "Нужен API-ключ",
  insufficient_metadata: "Недостаточно исходных данных",
  provider_cooldown: "Пауза после ошибок",
  provider_unavailable: "Источник недоступен",
  provider_unauthorized: "Нет доступа",
  budget_exhausted: "Достигнут лимит",
  rate_limited: "Ограничение частоты",
  invalid_audio: "Ошибка аудио",
};
const text = (value: unknown) => (Array.isArray(value) ? value.join(", ") : String(value || ""));
export function AnalysisPanel({
  track,
  enabled,
  recognitionEnabled,
  asrEnabled,
  busy,
  changed,
  onJob,
  onRefresh,
}: {
  track: Track;
  enabled: boolean;
  recognitionEnabled: boolean;
  asrEnabled: boolean;
  busy: boolean;
  changed: boolean;
  onJob: (kind: string, params?: Record<string, unknown>) => Promise<void>;
  onRefresh: () => void;
}) {
  const analysis = track.analysis;
  const [allAlignmentLines, setAllAlignmentLines] = useState(false);
  const [asrLanguage, setAsrLanguage] = useState("");
  const asrParams = asrLanguage ? { language: asrLanguage } : undefined;
  const alignmentLines = analysis?.alignment?.lines || [];
  const uncertainLines = alignmentLines.filter(
    (line) => line.start_ms == null || !!line.low_probability_words,
  );
  const hasHints = !!text(track.tags?.title).trim() && !!text(track.tags?.artist).trim();
  const needsAlbumSearch =
    hasHints &&
    !track.possible_mix &&
    !track.cue_album_image &&
    (!track.has_art || !text(track.tags?.album).trim());
  const SearchContainer = analysis?.candidates.length ? "details" : "section";
  const timingStatus = alignmentStatus(analysis);
  return (
    <section className="panel analysis-panel">
      <div className="panel-heading">
        <div>
          <h2>Источники и совпадения</h2>
          <p className="muted small">
            Поиск по каталогам и аудиоотпечатку. Для каждого варианта показан источник.
          </p>
        </div>
        <button
          className="icon-button"
          disabled={changed}
          onClick={onRefresh}
          aria-label="Обновить результаты поиска"
        >
          <RefreshCw size={18} />
        </button>
      </div>
      <SearchContainer className="workflow-job-details source-search-actions">
        {analysis?.candidates.length ? (
          <summary>Найти другие варианты</summary>
        ) : (
          <h3>Найти данные</h3>
        )}
        <p className="muted small">
          {hasHints
            ? "Исполнитель и название известны: начните с поиска по каталогам."
            : "Если песня неизвестна, начните с распознавания аудио."}{" "}
          Поиск не изменяет файл.
        </p>
        <div className="button-row">
          <button
            className={`button ${hasHints ? "secondary" : "primary"}`}
            disabled={
              !recognitionEnabled || busy || changed || !!track.input_error || track.cue_album_image
            }
            onClick={() => void onJob("recognize")}
          >
            <Search size={18} />
            Распознать песню по аудио
          </button>
          <button
            className={`button ${hasHints ? "primary" : "secondary"}`}
            disabled={!enabled || busy || changed || !!track.input_error}
            onClick={() => void onJob("analyze")}
          >
            Найти теги, обложку и текст
          </button>
        </div>
        {enabled && needsAlbumSearch && (
          <details className="workflow-job-details">
            <summary>Уточнить поиск по альбому</summary>
            <p className="muted small">
              Если точный альбом известен, найдите его издания и обложку. Результат появится в
              источниках для сравнения.
            </p>
            <form
              key={`${track.id}:${track.sha256}`}
              onSubmit={(event) => {
                event.preventDefault();
                const album = String(new FormData(event.currentTarget).get("album") || "").trim();
                if (album && !busy && !changed && !track.input_error) {
                  void onJob("analyze", { release_search: { album, revision: track.sha256 } });
                }
              }}
            >
              <label>
                Название альбома
                <input
                  name="album"
                  required
                  maxLength={180}
                  defaultValue={text(track.tags?.album)}
                  disabled={busy || changed || !!track.input_error}
                />
              </label>
              <button
                className="button secondary"
                disabled={busy || changed || !!track.input_error}
              >
                <Search size={18} />
                Найти издания альбома
              </button>
            </form>
          </details>
        )}
        {!enabled && <span className="muted small">Поиск по каталогам выключен</span>}
      </SearchContainer>
      <details className="workflow-job-details">
        <summary>Таймкоды, содержимое аудио и громкость</summary>
        <div className="button-row">
          <button
            className="button secondary"
            disabled={busy || !!track.input_error || track.cue_album_image}
            onClick={() => void onJob("replaygain")}
          >
            Измерить ReplayGain
          </button>
          <span className="muted small">Аудиосэмплы не меняются</span>
        </div>
        <div className="button-row asr-tools">
          <label>
            Язык записи
            <select
              value={asrLanguage}
              disabled={!asrEnabled || busy || !!track.input_error}
              onChange={(event) => setAsrLanguage(event.target.value)}
            >
              <option value="">Определить автоматически</option>
              <option value="ru">Русский</option>
              <option value="en">Английский</option>
            </select>
          </label>
          <button
            className="button secondary"
            disabled={!asrEnabled || busy || !!track.input_error}
            onClick={() => void onJob("transcribe", asrParams)}
          >
            Проверить содержимое аудио
          </button>
          <button
            className="button secondary"
            disabled={
              !asrEnabled || busy || changed || !(track.lrc || track.lyrics || track.proposal?.lrc)
            }
            onClick={() => void onJob("align", asrParams)}
          >
            Свести текст с аудио
          </button>
        </div>
      </details>
      {instrumentalTextConflict(track) && (
        <div className="notice warning">
          <CircleAlert size={19} />
          <p>
            В названии есть пометка инструментальной версии, но источник предложил слова. Сначала
            проверьте, есть ли в этой записи вокал и относится ли к ней текст. Одна пометка в
            названии не определяет содержимое аудио.
          </p>
        </div>
      )}
      {timingStatus && (
        <div className="notice info" role="status">
          <CircleAlert size={19} />
          <div>
            <strong>{timingStatus.title}</strong>
            <p className="small">{timingStatus.detail}</p>
          </div>
        </div>
      )}
      {track.input_error && (
        <p className="notice error" role="alert">
          <CircleAlert size={18} />
          {track.input_error.message}
        </p>
      )}
      {analysis?.asr && (
        <details className="asr-result">
          <summary>
            ASR: черновик · {analysis.asr.model} · {analysis.asr.language}
          </summary>
          <p className="muted small">
            Независимая расшифровка. Ошибки слов и пропуски вокала возможны; автоматическая запись
            выключена.
          </p>
          {!!analysis.asr.segments?.some((s) => s.uncertain) && (
            <p className="warning-text small">
              Есть фрагменты с низкой уверенностью. Проверьте их по аудио.
            </p>
          )}
          <pre>
            {analysis.asr.text ||
              "Надежно распознанных слов нет. Это не доказывает отсутствие вокала."}
          </pre>
        </details>
      )}
      {analysis?.alignment && (
        <details className="asr-result">
          <summary>Сопоставлено строк: {Math.round(analysis.alignment.coverage * 100)}%</summary>
          <p className="muted small">
            Особого внимания требуют {uncertainLines.length} из {alignmentLines.length} строк.
            Остальные совпадения также требуют проверки на слух.
          </p>
          <label className="small">
            <input
              type="checkbox"
              checked={allAlignmentLines}
              onChange={(event) => setAllAlignmentLines(event.target.checked)}
            />{" "}
            Показать все строки
          </label>
          {(allAlignmentLines ? alignmentLines : uncertainLines).map((line, index) => (
            <p key={index} className="small">
              <strong>
                {line.start_ms == null
                  ? "Нет надежного времени"
                  : (line.start_ms / 1000).toFixed(2) + " с"}
              </strong>{" "}
              · {line.text}
              {!!line.low_probability_words && (
                <span className="warning-text"> · Проверьте слова и начало строки</span>
              )}
            </p>
          ))}
          <p className="muted small">Несопоставленные строки не получают выдуманных таймкодов.</p>
        </details>
      )}
      {analysis && !analysis.candidates.length && (
        <p className="notice info">
          Подходящих вариантов пока нет. Проверьте исполнителя и название, прослушайте запись или
          используйте аудиоотпечаток. Можно заполнить поля вручную или вернуться к записи позже.
        </p>
      )}
      {analysis && (
        <>
          <details className="source-status-details">
            <summary>Состояние источников ({analysis.sources.length})</summary>
            <div className="analysis-sources">
              {analysis.sources.map((s, i) => (
                <span className="badge" key={s.provider + i}>
                  {s.provider}: {states[s.state] || s.state}
                </span>
              ))}
            </div>
          </details>
          {analysis.stale && <p className="warning-text">Файл изменился. Повторите поиск.</p>}
          {analysis.query_source === "filename_hint" && (
            <p className="muted small">Для запроса использованы подсказки из имени файла.</p>
          )}
          {analysis.query_source === "audio_fingerprint" && (
            <p className="muted small">
              Поиск текста и релиза выполнен по результату распознавания аудио.
            </p>
          )}
          {analysis.query_source === "album_hint" && (
            <p className="muted small">Поиск изданий выполнен по указанному альбому.</p>
          )}
          {!!analysis.candidates.length && (
            <SourceCandidatePicker
              key={`${track.id}:${track.sha256}:${analysis.updated_at || ""}`}
              track={track}
              changed={changed}
              onRefresh={onRefresh}
            />
          )}
        </>
      )}
      <details className="workflow-job-details">
        <summary>Проверки качества предложения</summary>
        <AutomaticQualityPanel
          trackId={track.id}
          revision={track.sha256}
          proposalId={track.proposal?.id}
          changed={changed}
        />
      </details>
    </section>
  );
}
