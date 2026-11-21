import { useEffect, useRef, useState } from "react";
import { api, messageFor, requestId } from "./api";
import { AudioCheckPanel } from "./AudioCheckPanel";
import { CollectionEvidence } from "./CollectionEvidence";
import type { Summary } from "./types";

type Run = {
  id?: string;
  folder?: string;
  profile?: string;
  state?: string;
  stage?: string;
  reason?: string;
  job_id?: string;
  scanned_files?: number;
  warnings?: Record<string, number>;
  autofill?: boolean;
  refines?: string;
  credit_only?: boolean;
  can_refine_lyrics?: boolean;
  supplements?: string;
  transcribed?: number;
  embedded_timings?: number;
  action?: string;
  processed?: number;
  applied?: number;
  published?: number;
  aligned?: number;
  scan_errors?: number;
  counts?: Record<string, number>;
  issues?: { code: string; message: string; count: number }[];
  skipped?: Record<string, string>;
};
type Listing = { folder: string; directories: string[]; next: string | null };
const stages: Record<string, string> = {
  scan: "Сканирование",
  recognize: "Распознавание музыки",
  analyze: "Поиск метаданных",
  review: "Проверка записей",
  autofill: "Заполнение коллекции",
  align: "Выравнивание текста по аудио",
  transcribe: "Проверка текста по аудио на ASR-сервере",
  apply: "Запись подтверждённых полей",
  publish: "Публикация подтверждённых файлов",
};
export function rememberedFolder() {
  try {
    return sessionStorage.getItem("phonoteka-source-folder") || "";
  } catch {
    return "";
  }
}

function ScanErrors({ id, count }: { id: string; count: number }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<{ file: string; message: string }[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open || items) return;
    const controller = new AbortController();
    setError("");
    api<{ items: { file: string; message: string }[] }>(
      "/preparation/" + encodeURIComponent(id) + "/scan-errors",
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) setItems(value.items);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(messageFor(e));
      });
    return () => controller.abort();
  }, [id, open, items]);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Ошибки сканирования ({count})</summary>
      {error ? (
        <p role="alert">{error}</p>
      ) : !items ? (
        <p>Загрузка…</p>
      ) : (
        <>
          <ul>
            {items.map((item) => (
              <li key={item.file}>
                <strong>{item.file}</strong> - {item.message}
              </li>
            ))}
          </ul>
          {count > items.length && <p>Полный список доступен в отчёте по файлам.</p>}
        </>
      )}
    </details>
  );
}

export function FolderPreparation({
  summary,
  folder,
  choose,
  onJobs,
  onReview,
}: {
  summary: Summary | null;
  folder: string;
  choose: (folder: string) => void;
  onJobs: () => void;
  onReview: () => void;
}) {
  const [listing, setListing] = useState<Listing | null>(null),
    [profile, setProfile] = useState("lyrics_required");
  const [run, setRun] = useState<Run>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [automatic, setAutomatic] = useState(true);
  const autofill = automatic && summary?.read_only === false;
  const [generation, setGeneration] = useState(0),
    [after, setAfter] = useState("");
  const action = useRef(false),
    intent = useRef<{ body: string; key: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setListing(null);
    setError("");
    setAfter("");
    api<Listing>("/work/folders?folder=" + encodeURIComponent(folder), {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setListing(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(messageFor(e));
      });
    return () => controller.abort();
  }, [folder, generation]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const state = await api<Run>("/preparation", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setRun(state);
        if (state.state === "active") timer = setTimeout(poll, 3000);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(messageFor(e));
          timer = setTimeout(poll, 10000);
        }
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [generation]);
  async function start() {
    if (action.current || !listing || !summary) return;
    action.current = true;
    setBusy(true);
    setError("");
    const body = JSON.stringify({ folder, profile, autofill });
    if (intent.current?.body !== body) intent.current = { body, key: requestId() };
    try {
      const state = await api<Run>("/preparation", {
        method: "POST",
        headers: { "Idempotency-Key": intent.current.key },
        body,
      });
      setRun(state);
      intent.current = null;
      setGeneration((n) => n + 1);
    } catch (e) {
      setError(messageFor(e));
    } finally {
      action.current = false;
      setBusy(false);
    }
  }
  async function more() {
    if (!listing?.next || after) return;
    const cursor = listing.next;
    setAfter(cursor);
    try {
      const value = await api<Listing>(
        "/work/folders?folder=" +
          encodeURIComponent(folder) +
          "&after=" +
          encodeURIComponent(cursor),
      );
      setListing((old) =>
        old?.folder === value.folder && old.next === cursor
          ? {
              ...value,
              directories: [...old.directories, ...value.directories],
            }
          : old,
      );
    } catch (e) {
      setError(messageFor(e));
    } finally {
      setAfter("");
    }
  }
  async function changeRun(operation: "stop" | "refine-lyrics" | "supplement") {
    if (action.current || !run.id) return;
    action.current = true;
    setBusy(true);
    try {
      setRun(
        await api<Run>("/preparation/" + encodeURIComponent(run.id) + "/" + operation, {
          method: "POST",
        }),
      );
      setGeneration((n) => n + 1);
    } catch (e) {
      setError(messageFor(e));
    } finally {
      action.current = false;
      setBusy(false);
    }
  }
  return (
    <section className="folder-preparation" aria-label="Источник и подготовка">
      <h3>Что организуем</h3>
      <p>Выберите папку на сервере. В подготовку войдут файлы во всех её подпапках.</p>
      <p>
        <strong>Выбрана папка</strong>{" "}
        <code>
          {summary?.roots?.work || "/work"}
          {folder ? "/" + folder : ""}
        </code>
      </p>
      <div className="button-row">
        {folder && (
          <button
            className="button secondary"
            onClick={() => choose(folder.split("/").slice(0, -1).join("/"))}
          >
            На уровень выше
          </button>
        )}
        {listing && listing.directories.length > 0 && (
          <label>
            Перейти в подпапку{" "}
            <select
              value=""
              onChange={(event) => {
                if (event.target.value)
                  choose([folder, event.target.value].filter(Boolean).join("/"));
              }}
            >
              <option value="">Выберите подпапку</option>
              {listing.directories.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
        {listing?.next && (
          <button className="button subtle" disabled={!!after} onClick={() => void more()}>
            Ещё папки
          </button>
        )}
      </div>
      {listing && (
        <p className="muted small">
          Папка доступна для чтения.
          {!listing.directories.length && " Вложенных папок нет."}
        </p>
      )}
      <label>
        Требования к готовой версии{" "}
        <select value={profile} onChange={(e) => setProfile(e.target.value)}>
          <option value="lyrics_required">Песня с текстом и проверенными таймкодами</option>
          <option value="instrumental">Инструментальная музыка</option>
          <option value="relaxed">С индивидуальными исключениями</option>
        </select>
      </label>
      <p className="muted small">
        {profile === "lyrics_required"
          ? "Проверяются название, исполнитель, альбом, обложка и синхронный текст."
          : profile === "instrumental"
            ? "Для каждого трека нужно подтвердить отсутствие вокала и указать причину."
            : "Для каждого трека потребуется указать причину исключения."}{" "}
        Профиль можно изменить при проверке записи.
      </p>
      {summary && !summary.read_only && (
        <label>
          <input
            type="checkbox"
            checked={automatic}
            onChange={(e) => setAutomatic(e.target.checked)}
          />
          Автоматически заполнить рабочую копию
        </label>
      )}
      {autofill ? (
        <p>
          Фонотека сама проверит файлы папки, запросит недостающие данные и запишет подтверждённые
          изменения. Спорные версии останутся в разделе «Нужно проверить». Вкладку можно закрыть.
        </p>
      ) : (
        <p>
          Сканирование
          {summary?.capabilities.recognition ? " → поиск по аудиоотпечатку" : ""}
          {summary?.capabilities.metadata_search ? " → поиск метаданных" : ""} → ручная проверка.
          Поиск использует включённые подключения.
        </p>
      )}
      {run.id && (
        <div className="notice info">
          <div>
            <strong>
              {run.state === "active"
                ? "Идёт подготовка"
                : run.state === "review"
                  ? "Подготовка завершена"
                  : "Подготовка остановлена"}
            </strong>
            <p>
              {run.folder || "Вся музыкальная папка"} ·{" "}
              {run.supplements
                ? "Обложки и сверка текстов по аудио"
                : run.refines
                  ? "Уточнение поиска текстов"
                  : stages[run.stage || ""] || run.stage}
            </p>
            {run.state === "review" && run.scanned_files === 0 && (
              <p>В этой папке не найдено поддерживаемых файлов. Выберите другую папку.</p>
            )}
            {run.autofill && run.processed !== undefined && (
              <div aria-live="polite">
                {run.refines ? (
                  <p>
                    Проверено {run.processed} из {run.scanned_files}. Найдены варианты с таймкодами:{" "}
                    {run.counts?.found || 0}. Без новых таймкодов: {run.counts?.not_found || 0}.
                    Пропущены: {run.counts?.excluded || 0}. Требуют проверки:{" "}
                    {run.counts?.review || 0}.
                  </p>
                ) : (
                  <>
                    <p>
                      Проверено {run.processed} из {run.scanned_files}. Изменены файлы:{" "}
                      {run.applied || 0}. Опубликованы в Ready: {run.published || 0}. Задач
                      выравнивания завершено: {run.aligned || 0}. Новых расшифровок:{" "}
                      {run.transcribed || 0}.
                    </p>
                    {!!run.embedded_timings && (
                      <p>Уже были таймкоды в файле: {run.embedded_timings}.</p>
                    )}
                    <p>
                      Все поля заполнены: {run.counts?.populated || 0}. Требуют проверки:{" "}
                      {run.counts?.review || 0}. Пропущены: {run.counts?.excluded || 0}. Ошибки
                      сканирования: {run.scan_errors || 0}.
                    </p>
                  </>
                )}
                {run.state === "active" && run.action && <p>{stages[run.action] || run.action}</p>}
                {!!run.issues?.length && (
                  <details>
                    <summary>Причины пропусков</summary>
                    <ul>
                      {run.issues.map((i) => (
                        <li key={i.code}>
                          {i.message} · {i.count}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {!!run.scan_errors && run.id && (
                  <ScanErrors key={run.id} id={run.id} count={run.scan_errors} />
                )}
                {(run.processed > 0 || !!run.scan_errors) && (
                  <a
                    href={"/api/preparation/" + encodeURIComponent(run.id || "") + "/report"}
                    download
                  >
                    Скачать результат по файлам (CSV)
                  </a>
                )}
              </div>
            )}
            {run.autofill && run.state === "active" && (
              <button
                className="button subtle"
                disabled={busy}
                onClick={() => void changeRun("stop")}
              >
                Остановить заполнение
              </button>
            )}
            {run.can_refine_lyrics && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void changeRun("refine-lyrics")}
              >
                Уточнить поиск текстов
              </button>
            )}
            {run.autofill &&
              run.state === "review" &&
              !run.supplements &&
              !run.credit_only &&
              !!run.scanned_files && (
                <div>
                  <button
                    className="button primary"
                    disabled={busy}
                    onClick={() => void changeRun("supplement")}
                  >
                    Дособрать обложки и сверить тексты всей папки
                  </button>
                  <p>
                    Используется сохранённый список файлов. Фонотека ищет обложки изданий и сверяет
                    найденные тексты по аудио. Спорные варианты останутся на проверке.
                  </p>
                </div>
              )}
            {run.id && run.autofill && run.state === "review" && (
              <CollectionEvidence key={run.id} id={run.id} />
            )}
            {run.supplements && (
              <a
                href={"/api/preparation/" + encodeURIComponent(run.supplements) + "/report"}
                download
              >
                Предыдущий отчёт (CSV)
              </a>
            )}
            {run.refines && (
              <a href={"/api/preparation/" + encodeURIComponent(run.refines) + "/report"} download>
                Отчёт исходного прохода (CSV)
              </a>
            )}
            {run.id && run.supplements && run.state === "review" && !!run.counts?.review && (
              <AudioCheckPanel key={run.id} id={run.id} onJobs={onJobs} durationReport />
            )}
            {run.id &&
              run.refines &&
              !run.supplements &&
              run.state === "review" &&
              !!run.counts?.found && <AudioCheckPanel key={run.id} id={run.id} onJobs={onJobs} />}
            {!!Object.keys(run.skipped || {}).length && (
              <small>
                {Object.entries(run.skipped || {})
                  .map(
                    ([kind, why]) =>
                      stages[kind] + (why === "disabled" ? " выключен" : " - новых файлов нет"),
                  )
                  .join(" · ")}
              </small>
            )}
            {run.warnings && (
              <p>
                Файлы, требующие внимания:{" "}
                {Object.entries(run.warnings)
                  .map(([kind, n]) => stages[kind] + " - " + n)
                  .join(", ")}
                . Откройте результаты в обработке.
              </p>
            )}
            {run.reason && (
              <p>
                {run.reason === "webhook_queue_full"
                  ? "Ожидаем место в очереди"
                  : run.reason === "authorization_changed"
                    ? "Изменился доступ к приложению"
                    : "Причина указана в задаче обработки"}
              </p>
            )}
            <button
              className="button subtle"
              disabled={run.state === "review" && run.scanned_files === 0}
              onClick={run.state === "review" ? onReview : onJobs}
            >
              {run.state === "review" ? "Перейти к проверке" : "Открыть обработку"}
            </button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert">
          {error}{" "}
          <button className="button subtle" onClick={() => setGeneration((n) => n + 1)}>
            Обновить
          </button>
        </p>
      )}
      <button
        className="button primary"
        disabled={busy || !listing || !summary || run.state === "active"}
        onClick={() => void start()}
      >
        {busy
          ? "Подождите…"
          : autofill
            ? "Организовать выбранную папку"
            : "Подготовить выбранную папку"}
      </button>
    </section>
  );
}
