import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check, CircleAlert } from "lucide-react";
import { api, messageFor } from "./api";
import type { Track, Candidate, CandidateRelease } from "./types";
import { ProviderCandidateUsage, type PolicyCandidate } from "./ProviderPolicyPanel";
import { fieldLabels } from "./TrackPolicyPanel";
import { useBackLayer } from "./useAppHistory";
import {
  candidateText as text,
  candidateDuration,
  lyricsSummary,
  groupCandidates,
  chooseCandidate,
  sourceNames,
} from "./sourceCandidates";
import "./SourceCandidatePicker.css";

const reasons: Record<string, string> = {
  album_differs: "Альбом или исполнитель отличается",
  confirm_album_group: "Проверьте состав альбома",
  replaygain_preview: "Измерение изменит только теги громкости",
  recording_version_unverified: "Песня узнана по аудио; версия и альбом не подтверждены",
  human_timing_review_required: "Таймкоды нужно проверить по вокалу",
  title_differs: "Название отличается",
  artist_differs: "Исполнитель отличается",
  duration_differs: "Длительность отличается",
  duration_unknown: "Длительность неизвестна",
  lyrics_out_of_range: "Таймкоды выходят за длительность",
  invalid_lyrics: "В таймкодах есть ошибки",
  confirm_release: "Проверьте релиз",
  confirm_album_cover: "Проверьте принадлежность обложки альбому",
};
type PreviewField = {
  field: string;
  before: unknown;
  after: unknown;
  changed: boolean;
  source: { provider?: string; candidate_id?: string };
};
function revealHeading(heading: HTMLHeadingElement | null) {
  heading?.focus({ preventScroll: true });
  heading?.scrollIntoView({
    block: "start",
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
  });
}
export type SelectionPreview = {
  preview_revision: string;
  fields: PreviewField[];
  blocked_fields: string[];
  omitted_fields: string[];
  replaces_proposal: boolean;
};

function PreviewValue({ field, value }: { field: string; value: unknown }) {
  if (field === "cover") return <span>{value ? "Есть обложка" : "Нет обложки"}</span>;
  if (field === "lrc")
    return (
      <details>
        <summary>{lyricsSummary(text(value))}</summary>
        <pre>{text(value)}</pre>
      </details>
    );
  return <span>{text(value) || "Не заполнено"}</span>;
}
export function SourceSelectionComparison({
  preview,
  excluded,
  disabled,
  onToggle,
}: {
  preview: SelectionPreview;
  excluded: string[];
  disabled: boolean;
  onToggle: (field: string) => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    revealHeading(heading.current);
  }, []);
  return (
    <section className="source-comparison" aria-label="Сравнение выбранных данных">
      <h3 ref={heading} tabIndex={-1}>
        Что войдёт в предложение
      </h3>
      <p className="muted small">
        Снимите отметку с полей, которые хотите оставить как есть. Закреплённые поля сохранятся.
      </p>
      {!preview.fields.length && (
        <p className="warning-text">
          Нет доступных изменений. Выберите другой вариант или проверьте закреплённые поля.
        </p>
      )}
      {preview.fields.map((field) => (
        <article className="source-field-diff" key={field.field}>
          <div>
            <label className="source-field-toggle">
              <input
                type="checkbox"
                checked={!excluded.includes(field.field)}
                disabled={disabled}
                onChange={() => onToggle(field.field)}
              />
              <strong>{fieldLabels[field.field] || field.field}</strong>
            </label>
            <span className="muted small">
              {sourceNames[field.source.provider || ""] || field.source.provider}
              {!field.changed && " · Уже в файле"}
            </span>
          </div>
          <div className="source-diff-values">
            <div>
              <small>Сейчас в файле</small>
              <PreviewValue field={field.field} value={field.before} />
            </div>
            <ArrowRight size={16} aria-hidden="true" />
            <div>
              <small>
                {excluded.includes(field.field) ? "Не войдёт в предложение" : "Предложено"}
              </small>
              <PreviewValue field={field.field} value={field.after} />
            </div>
          </div>
        </article>
      ))}
      {!!preview.blocked_fields.length && (
        <p className="notice info">
          Закреплены и сохранятся:{" "}
          {preview.blocked_fields.map((field) => fieldLabels[field] || field).join(", ")}.
        </p>
      )}
      {!!preview.omitted_fields.length && (
        <p className="warning-text">
          Не войдут в предложение:{" "}
          {preview.omitted_fields.map((field) => fieldLabels[field] || field).join(", ")}. Проверьте
          формат текста, таймкоды и закреплённые строки.
        </p>
      )}
      {preview.replaces_proposal && (
        <p className="warning-text small">
          Сохранение заменит текущее предложение целиком, включая его текст. Вернуться к выбору
          можно без сохранения.
        </p>
      )}
    </section>
  );
}

function CandidateOption({
  candidate,
  selected,
  disabled,
  name,
  index,
  sameLyricsAs,
  onChoose,
  trackId,
  releaseDetails,
  embeddedRelease,
}: {
  candidate: Candidate;
  selected: boolean;
  disabled: boolean;
  name: string;
  index: number;
  sameLyricsAs?: number;
  onChoose: () => void;
  trackId: string;
  releaseDetails?: CandidateRelease;
  embeddedRelease?: boolean;
}) {
  const usageCandidate = candidate as PolicyCandidate;
  const provider = sourceNames[candidate.provider] || candidate.provider;
  const coverLabel = releaseDetails?.title || candidate.album || "Обложка альбома";
  const label = `${candidate.kind === "lyrics" ? lyricsSummary(candidate.lrc || candidate.plain_lyrics) : candidate.kind === "cover" ? coverLabel : candidate.kind === "release" ? `Альбом: ${text(candidate.tags?.album) || candidate.title || "не указан"}` : text(candidate.tags?.title) || candidate.title || "Данные записи"}`;
  const identity = text(candidate.tags?.artist) || candidate.artist || "Исполнитель не указан";
  const isRelease = candidate.kind === "release";
  const edition = [
    releaseDetails?.date || (isRelease ? text(candidate.tags?.date) : ""),
    releaseDetails?.country,
    isRelease && candidate.release_id ? `ID ${candidate.release_id.slice(0, 8)}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const optionDetail =
    candidate.kind === "cover"
      ? edition || candidate.release_id || ""
      : isRelease
        ? edition || "Сведения об издании не указаны"
        : identity;
  const delta = candidate.match.duration_delta;
  return (
    <article className={`source-option ${selected ? "selected" : ""}`}>
      <label className="source-option-choice">
        <input
          type="radio"
          name={name}
          checked={selected}
          disabled={disabled || usageCandidate.transient_only}
          onChange={onChoose}
        />
        <span>
          <span className="source-option-provider">
            {provider} · вариант {index + 1}
            {selected && " · Выбран"}
          </span>
          <strong>{label}</strong>
          <span className="muted small">
            {optionDetail}
            {candidate.kind === "lyrics" &&
              ` · ${text(candidate.tags?.title) || candidate.title || "Название не указано"}`}
          </span>
        </span>
      </label>
      {embeddedRelease && <p className="muted small">ID издания совпадает с тегом файла.</p>}
      {candidate.kind !== "cover" && !isRelease && (
        <dl className="source-option-facts">
          <div>
            <dt>Альбом</dt>
            <dd>{text(candidate.tags?.album) || candidate.album || "Не указан"}</dd>
          </div>
          <div>
            <dt>Длительность</dt>
            <dd>
              {candidateDuration(candidate.duration)}
              {delta != null && delta > 0 && (
                <span className="warning-text"> · разница {delta.toFixed(1)} с</span>
              )}
              {delta === 0 && " · Совпадает"}
            </dd>
          </div>
        </dl>
      )}
      {!!candidate.match.reasons.length && (
        <p className="source-option-warning small">
          <CircleAlert size={15} aria-hidden="true" />
          {candidate.match.reasons.map((reason) => reasons[reason] || reason).join(". ")}
        </p>
      )}
      {!candidate.match.reasons.length && (
        <p className="muted small">Явных расхождений не найдено. Сверьте версию по аудио.</p>
      )}
      {candidate.cover && (
        <img
          className="candidate-cover"
          src={candidate.cover}
          alt={releaseDetails?.title ? `Обложка ${releaseDetails.title}` : "Предлагаемая обложка"}
          loading="lazy"
        />
      )}
      {(candidate.lrc || candidate.plain_lyrics) && (
        <>
          <p className="source-lyrics-excerpt small">
            {(candidate.lrc || candidate.plain_lyrics || "")
              .split(/\r?\n/)
              .filter((line) => line.trim())
              .slice(0, 2)
              .join(" / ")
              .slice(0, 160)}
          </p>
          {sameLyricsAs != null && (
            <p className="muted small">
              Текст и таймкоды совпадают с вариантом {sameLyricsAs + 1} в этом разделе.
            </p>
          )}
          <details className="source-lyrics-preview">
            <summary>Сверить слова{candidate.lrc ? " и таймкоды" : ""}</summary>
            <pre>{candidate.lrc || candidate.plain_lyrics}</pre>
            <p className="muted small">Совпадение с вокалом ещё не проверено.</p>
          </details>
        </>
      )}
      {candidate.instrumental_claim && (
        <p className="warning-text small">
          Источник считает запись инструментальной. Проверьте наличие вокала.
        </p>
      )}
      <details className="source-option-details">
        <summary>Подробности и источник</summary>
        <dl className="source-option-facts">
          {Object.entries(candidate.tags || {}).map(([field, value]) => (
            <div key={field}>
              <dt>{fieldLabels[field] || field}</dt>
              <dd>{text(value) || "Не заполнено"}</dd>
            </div>
          ))}
        </dl>
        {candidate.release && <pre>{JSON.stringify(candidate.release, null, 2)}</pre>}
        {candidate.source_url && (
          <a href={candidate.source_url} target="_blank" rel="noreferrer">
            Открыть запись в {provider}
          </a>
        )}
        {candidate.source_path && <p className="small break-word">{candidate.source_path}</p>}
        {!usageCandidate.transient_only && usageCandidate.usage && (
          <ProviderCandidateUsage trackId={trackId} candidate={usageCandidate} />
        )}
      </details>
      {usageCandidate.transient_only ? (
        <ProviderCandidateUsage trackId={trackId} candidate={usageCandidate} />
      ) : (
        <>
          {usageCandidate.usage?.allow_embed === false && (
            <p className="warning-text small">Источник не разрешает запись этих данных в файл.</p>
          )}
          {usageCandidate.usage?.allow_export === false && (
            <p className="warning-text small">Экспорт этих данных ограничен источником.</p>
          )}
        </>
      )}
    </article>
  );
}

export function SourceCandidatePicker({
  track,
  changed,
  onRefresh,
}: {
  track: Track;
  changed: boolean;
  onRefresh: () => void;
}) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [preview, setPreview] = useState<SelectionPreview | null>(null);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [pending, setPending] = useState<"preview" | "save" | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const returnToChoices = () => {
    setPreview(null);
    revealHeading(heading.current);
  };
  useBackLayer(!!preview, () => {
    if (!pending) returnToChoices();
  });
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const analysis = track.analysis!;
  const groups = groupCandidates(analysis.candidates);
  const releaseDetailsById = new Map<string, CandidateRelease>();
  for (const source of analysis.candidates) {
    if (source.provider !== "musicbrainz") continue;
    for (const release of source.releases || []) {
      if (release.title && !releaseDetailsById.has(release.id))
        releaseDetailsById.set(release.id, release);
    }
  }
  const fileRelease = text(track.tags.musicbrainz_albumid).toLowerCase();
  const disabled = changed || analysis.stale || !track.can_write || !!pending;
  const choose = (kind: string, id?: string) => {
    setChosen((old) => chooseCandidate(old, analysis.candidates, kind, id));
    setPreview(null);
    setError("");
    setSaved(false);
  };
  async function submit(save: boolean) {
    setPending(save ? "save" : "preview");
    setError("");
    try {
      const response = await api<SelectionPreview>(
        `/tracks/${encodeURIComponent(track.id)}/analysis/${save ? "select" : "preview"}`,
        {
          method: "POST",
          headers: { "If-Match": `"${track.sha256}"` },
          body: JSON.stringify({
            candidate_ids: chosen,
            ...(save
              ? { preview_revision: preview!.preview_revision, excluded_fields: excluded }
              : {}),
          }),
        },
      );
      if (!alive.current) return;
      if (save) {
        setChosen([]);
        setPreview(null);
        setSaved(true);
        onRefresh();
      } else {
        setExcluded([]);
        setPreview(response);
      }
    } catch (err) {
      if (alive.current) {
        setError(messageFor(err));
        setPreview(null);
      }
    } finally {
      if (alive.current) setPending(null);
    }
  }
  return (
    <div className="source-picker">
      <div className="source-picker-intro">
        <h3 ref={heading} tabIndex={-1}>
          Выберите, что добавить
        </h3>
        <p>
          Можно выбрать по одному варианту в нужных разделах. Затем сравните изменения перед
          сохранением.
        </p>
        <p className="small">
          Если теги уже верны, начните с текста или обложки. Выбирать каждый раздел не обязательно.
        </p>
        <p className="muted small">
          Сейчас в файле: {text(track.tags.title) || "Название не указано"} ·{" "}
          {text(track.tags.artist) || "Исполнитель не указан"} ·{" "}
          {text(track.tags.album) || "Альбом не указан"} · {candidateDuration(track.duration)}
        </p>
      </div>
      {groups.map((group) => (
        <fieldset className="source-group" key={group.kind} disabled={disabled}>
          <legend>
            {group.title}
            <span>{group.candidates.length}</span>
          </legend>
          <p className="muted small">{group.help}</p>
          <label className="source-skip">
            <input
              type="radio"
              name={`source-${track.id}-${group.kind}`}
              checked={!group.candidates.some((c) => chosen.includes(c.id))}
              onChange={() => choose(group.kind)}
            />
            Не использовать варианты этого раздела
          </label>
          <div className="source-option-grid">
            {group.candidates.map((candidate, index) => (
              <CandidateOption
                key={candidate.id}
                candidate={candidate}
                selected={chosen.includes(candidate.id)}
                disabled={!!disabled}
                name={`source-${track.id}-${group.kind}`}
                index={index}
                sameLyricsAs={(() => {
                  const words = candidate.lrc || candidate.plain_lyrics;
                  const first = words
                    ? group.candidates.findIndex((c) => (c.lrc || c.plain_lyrics) === words)
                    : -1;
                  return first >= 0 && first < index ? first : undefined;
                })()}
                onChoose={() => choose(group.kind, candidate.id)}
                trackId={track.id}
                releaseDetails={
                  (candidate.kind === "release" || candidate.provider === "coverartarchive") &&
                  candidate.release_id
                    ? releaseDetailsById.get(candidate.release_id)
                    : undefined
                }
                embeddedRelease={
                  (candidate.kind === "release" || candidate.provider === "coverartarchive") &&
                  !!fileRelease &&
                  candidate.release_id?.toLowerCase() === fileRelease
                }
              />
            ))}
          </div>
        </fieldset>
      ))}
      <div className="source-selection-footer">
        <p className="small" role="status">
          {chosen.length
            ? `Выбрано разделов: ${chosen.length}. ${groups
                .filter((group) => group.candidates.some((c) => chosen.includes(c.id)))
                .map((group) => group.title)
                .join(", ")}.`
            : "Выберите вариант хотя бы в одном разделе. Остальные разделы можно пропустить."}
        </p>
        {!preview && (
          <button
            className="button primary"
            disabled={disabled || !chosen.length}
            onClick={() => void submit(false)}
          >
            <ArrowRight size={17} />
            {pending === "preview" ? "Сравниваем..." : "Сравнить изменения"}
          </button>
        )}
        <span className="muted small">Сравнение не изменяет файл и текущее предложение.</span>
      </div>
      {preview && (
        <>
          <SourceSelectionComparison
            preview={preview}
            excluded={excluded}
            disabled={disabled}
            onToggle={(field) =>
              setExcluded((old) =>
                old.includes(field) ? old.filter((value) => value !== field) : [...old, field],
              )
            }
          />
          <div className="button-row">
            <button
              className="button primary"
              disabled={disabled || excluded.length === preview.fields.length}
              onClick={() => void submit(true)}
            >
              <Check size={17} />
              {pending === "save" ? "Сохраняем..." : "Сохранить как предложение"}
            </button>
            <button className="button secondary" disabled={!!pending} onClick={returnToChoices}>
              Вернуться к выбору
            </button>
          </div>
          <p className="muted small">
            Затем откройте «Теги и обложка» или «Текст» для проверки и записи в файл. Сейчас
            аудиофайл не меняется.
          </p>
        </>
      )}
      {saved && (
        <p className="notice info" role="status">
          Предложение сохранено. Откройте «Теги и обложка» или «Текст», чтобы проверить его и
          записать в файл.
        </p>
      )}
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {changed && <p className="warning-text">Сначала сохраните черновик редактора.</p>}
      {!track.can_write && <p className="warning-text">Для этого файла запись недоступна.</p>}
    </div>
  );
}
