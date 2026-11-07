import { useEffect, useRef, useState } from "react";
import { FolderCheck, RefreshCw } from "lucide-react";
import { api, messageFor } from "./api";

import type { Job } from "./types";
import { isActiveJob, jobStates } from "./jobs";
type ExportJob = Job & {
  export_progress?: { copied: number; total: number; eta_seconds: number | null };
};
type Snapshot = {
  layout?: "source" | "artist_album";
  snapshot_available?: boolean;
  id: string;
  created_at: string;
  files: Array<{ path: string; sha256: string }>;
  total_bytes: number;
};
type Status = {
  export: Snapshot | null;
  ready_count: number;
  stale_files?: number;
  receipts: Array<{ id: string; device: string; state: string; created_at: string }>;
};

export function DeliveryPanel({
  onExport,
  busy,
  onPlan,
  revision,
}: {
  onExport: (layout: "source" | "artist_album") => Promise<Job | null>;
  busy: boolean;
  onPlan: () => void;
  revision: string;
}) {
  const [job, setJob] = useState<ExportJob | null>(null),
    [starting, setStarting] = useState(false),
    [watch, setWatch] = useState(0);
  const [finished, setFinished] = useState("");
  const [catalogFolders, setCatalogFolders] = useState(true);
  const admission = useRef(false);
  const fetchSeq = useRef(0),
    snapshotKey = useRef("");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function follow() {
      try {
        let identity = sessionStorage.getItem("phonoteka-export-job");
        if (!identity) {
          const list = await api<{ items: Job[] }>(
            "/jobs?active_only=true&kind=export_ready&page_size=1",
            { signal: controller.signal },
          );
          identity = list.items[0]?.id || null;
        }
        if (!identity || controller.signal.aborted) return;
        const latest = await api<ExportJob>("/jobs/" + encodeURIComponent(identity), {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setJob(latest);
        sessionStorage.setItem("phonoteka-export-job", latest.id);
        if (isActiveJob(latest)) {
          timer = setTimeout(follow, 1600);
          return;
        }
        if (latest.state === "succeeded") {
          const id = latest.results?.find((r) => typeof r.export_id === "string")?.export_id;
          setFinished(typeof id === "string" ? id : "");
          await load();
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(messageFor(e));
          timer = setTimeout(follow, 4500);
        }
      }
    }
    void follow();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [watch]);
  async function startExport() {
    if (admission.current || (job && isActiveJob(job))) return;
    admission.current = true;
    setStarting(true);
    try {
      const admitted = await onExport(catalogFolders ? "artist_album" : "source");
      if (admitted) {
        sessionStorage.setItem("phonoteka-export-job", admitted.id);
        setJob(admitted);
        setFinished("");
        setWatch((n) => n + 1);
      }
    } catch (e) {
      setError(messageFor(e));
    } finally {
      admission.current = false;
      setStarting(false);
    }
  }
  const [status, setStatus] = useState<Status | null>(null),
    [error, setError] = useState("");
  const [device, setDevice] = useState("Poweramp"),
    [checksums, setChecksums] = useState<Record<string, string>>({});
  const [checked, setChecked] = useState(false),
    [saving, setSaving] = useState(false);
  async function load() {
    try {
      const seq = ++fetchSeq.current;
      const data = await api<Status>("/exports/latest");
      if (seq !== fetchSeq.current) return;
      const id = data.export?.id || "";
      if (snapshotKey.current !== id) {
        snapshotKey.current = id;
        setChecksums({});
        setChecked(false);
      }
      setStatus(data);
      setError("");
    } catch (problem) {
      setError(messageFor(problem));
    }
  }
  useEffect(() => {
    void load();
  }, [revision]);
  async function importFile(file?: File) {
    if (!file) return;
    const captured = snapshotKey.current;
    try {
      if (file.size > 2 * 1024 * 1024) throw Error("Отчет превышает 2 МиБ");
      const doc = JSON.parse(await file.text());
      const hashes = doc.files
        ? Object.fromEntries(
            doc.files.map((item: { path: string; sha256: string }) => [item.path, item.sha256]),
          )
        : doc;
      if (captured !== snapshotKey.current) return;
      setChecksums(hashes);
      setChecked(false);
      setError("");
    } catch (problem) {
      setError(messageFor(problem));
    }
  }
  async function save() {
    if (!status?.export || saving) return;
    setSaving(true);
    try {
      await api("/deliveries", {
        method: "POST",
        body: JSON.stringify({
          export_id: status.export.id,
          device,
          checksums,
          poweramp_checked: checked,
        }),
      });
      await load();
    } catch (problem) {
      setError(messageFor(problem));
    } finally {
      setSaving(false);
    }
  }
  const labels: Record<string, string> = {
    copy_reported: "Копирование отмечено вручную",
    client_hashes_match: "Контрольные суммы клиента совпали",
    poweramp_owner_verified: "Poweramp проверен вручную",
  };
  const snapshot = status?.export;
  const exportButton = (
    <button
      className={snapshot?.snapshot_available ? "button secondary" : "button primary"}
      disabled={busy || starting || (!!job && isActiveJob(job))}
      onClick={() => void startExport()}
    >
      <FolderCheck size={18} />
      {starting || (job && isActiveJob(job))
        ? "Готовим файлы…"
        : snapshot
          ? "Подготовить новый снимок"
          : "Подготовить файлы для переноса"}
    </button>
  );
  return (
    <section className="panel settings-panel delivery-panel">
      <div className="panel-heading">
        <div>
          <h2>Заберите готовую музыку</h2>
          <p className="muted small">Аудиофайлы и плейлист для переноса на телефон.</p>
        </div>
        <button
          className="icon-button"
          aria-label="Обновить результат экспорта"
          onClick={() => void load()}
        >
          <RefreshCw size={18} />
        </button>
      </div>
      {status && status.ready_count === 0 && (
        <div>
          <p>
            Пока нет опубликованных файлов. В плане можно принять версии для сохранения и увидеть,
            что ещё нужно проверить.
          </p>
          <button className="button primary" onClick={onPlan}>
            Открыть план сохранения
          </button>
        </div>
      )}
      {status && status.ready_count > 0 && (
        <label className="checkline">
          <input
            type="checkbox"
            checked={catalogFolders}
            disabled={busy || starting || (!!job && isActiveJob(job))}
            onChange={(event) => setCatalogFolders(event.target.checked)}
          />
          Новый снимок: папки Исполнитель / Альбом / Трек
        </label>
      )}
      {status && status.ready_count > 0 && !snapshot?.snapshot_available && exportButton}
      {job && (
        <div role="status">
          <strong>Подготовка снимка · {jobStates[job.state] || job.state}</strong>
          {isActiveJob(job) && job.export_progress ? (
            <p>
              Скопировано {job.export_progress.copied} из {job.export_progress.total} ·{" "}
              {Math.floor((100 * job.export_progress.copied) / (job.export_progress.total || 1))}%
              {job.export_progress.eta_seconds != null
                ? " · осталось около " + job.export_progress.eta_seconds + " с"
                : ""}
            </p>
          ) : isActiveJob(job) ? (
            <p>Проверяем состав и размер файлов.</p>
          ) : job.error ? (
            <p>{job.error}</p>
          ) : null}
          {finished && finished !== snapshot?.id && (
            <a
              className="button secondary"
              href={"/api/exports/" + finished + "/collection.zip"}
              download
            >
              Скачать результат этой задачи
            </a>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="warning-text">
          {error}
        </p>
      )}
      {status && status.ready_count > 0 && !snapshot && (
        <p className="muted small">
          Нажмите «Подготовить файлы для переноса». Здесь появится ссылка для скачивания.
        </p>
      )}
      {snapshot && (
        <>
          <div className="delivery-path">
            <strong>
              Файлов: {snapshot.files.length} ·{" "}
              {snapshot.total_bytes < 1024 ** 2
                ? Math.ceil(snapshot.total_bytes / 1024) + " КиБ"
                : (snapshot.total_bytes / 1024 ** 2).toFixed(1) + " МиБ"}
            </strong>
            <p className="muted small">
              Снимок от {new Date(snapshot.created_at).toLocaleString("ru-RU")}
              {snapshot.layout === "artist_album"
                ? " · Исполнитель / Альбом / Трек"
                : " · Исходные имена и папки"}
            </p>
          </div>
          {snapshot.snapshot_available && (
            <>
              <a
                className="button primary"
                href={"/api/exports/" + snapshot.id + "/collection.zip"}
                download
              >
                Скачать коллекцию ZIP
              </a>
              <ol>
                <li>Скопируйте ZIP на телефон и распакуйте целиком.</li>
                <li>
                  В Poweramp добавьте распакованную папку в музыкальную библиотеку. Плейлист
                  откроется вместе с аудиофайлами.
                </li>
              </ol>
              {status && status.ready_count > 0 && exportButton}
            </>
          )}
          <details>
            <summary>Плейлист и контрольные суммы</summary>
            <div className="button-row">
              <a href={"/api/exports/" + snapshot.id + "/playlist.m3u8"} download>
                Скачать плейлист
              </a>
              <a href={"/api/exports/" + snapshot.id + "/manifest.json"} download>
                Скачать манифест SHA-256
              </a>
            </div>
          </details>
          {!!status?.stale_files && (
            <p className="warning-text">
              После создания снимка изменились версии {status.stale_files} файлов. Для переноса
              новых версий подготовьте следующий снимок.
            </p>
          )}
          {snapshot.snapshot_available ? (
            <details>
              <summary>Скачать аудиофайлы ({snapshot.files.length})</summary>
              <div className="snapshot-files">
                {snapshot.files.map((file) => (
                  <a
                    key={file.path}
                    href={`/api/exports/${snapshot.id}/files/${file.path.split("/").map(encodeURIComponent).join("/")}`}
                    download
                  >
                    {file.path}
                  </a>
                ))}
              </div>
            </details>
          ) : (
            <p className="warning-text">
              Этот старый экспорт содержит только список файлов. Создайте новый снимок с аудио.
            </p>
          )}
          <details className="delivery-receipt">
            <summary>Перенос и проверка в Poweramp</summary>
            <p>
              Добавьте папку с музыкой в библиотеку Poweramp. Проверьте теги, обложки, строки
              текста, перемотку и громкость. Наличие снимка на сервере не подтверждает перенос на
              устройство.
            </p>
            <label>
              Устройство
              <input
                value={device}
                maxLength={80}
                onChange={(event) => setDevice(event.target.value)}
              />
            </label>
            <details>
              <summary>Сверить файлы устройства по SHA-256</summary>
              <label>
                Отчет SHA256, рассчитанный на устройстве
                <input
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => void importFile(event.target.files?.[0])}
                />
              </label>
              <p className="muted small">
                Загружено контрольных сумм: {Object.keys(checksums).length}. Без отчета можно
                отметить только факт копирования.
              </p>
              <label>
                <input
                  type="checkbox"
                  disabled={!Object.keys(checksums).length}
                  checked={checked}
                  onChange={(event) => setChecked(event.target.checked)}
                />{" "}
                Проверил теги, обложку, строки, перемотку и громкость в Poweramp офлайн
              </label>
            </details>
            <button
              className="button secondary"
              disabled={saving || !device.trim()}
              onClick={() => void save()}
            >
              {Object.keys(checksums).length
                ? "Сохранить результат проверки"
                : "Отметить копирование"}
            </button>
            {status?.receipts.map((receipt) => (
              <p key={receipt.id} className="small">
                {receipt.device}: {labels[receipt.state] || receipt.state} ·{" "}
                {new Date(receipt.created_at).toLocaleString("ru-RU")}
              </p>
            ))}
          </details>
        </>
      )}
    </section>
  );
}
