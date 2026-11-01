import { useEffect, useState } from "react";
import { FolderCheck, RefreshCw } from "lucide-react";
import { api, messageFor } from "./api";

type Snapshot = {
  snapshot_available?: boolean;
  id: string;
  created_at: string;
  files: Array<{ path: string; sha256: string }>;
  total_bytes: number;
};
type Status = {
  export: Snapshot | null;
  stale_files?: number;
  receipts: Array<{ id: string; device: string; state: string; created_at: string }>;
};

export function DeliveryPanel({ onExport, busy }: { onExport: () => void; busy: boolean }) {
  const [status, setStatus] = useState<Status | null>(null),
    [error, setError] = useState("");
  const [device, setDevice] = useState("Poweramp"),
    [checksums, setChecksums] = useState<Record<string, string>>({});
  const [checked, setChecked] = useState(false),
    [saving, setSaving] = useState(false);
  async function load() {
    try {
      setStatus(await api<Status>("/exports/latest"));
      setError("");
    } catch (problem) {
      setError(messageFor(problem));
    }
  }
  useEffect(() => {
    void load();
  }, []);
  async function importFile(file?: File) {
    if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024) throw Error("Отчет превышает 2 МиБ");
      const doc = JSON.parse(await file.text());
      const hashes = doc.files
        ? Object.fromEntries(
            doc.files.map((item: { path: string; sha256: string }) => [item.path, item.sha256]),
          )
        : doc;
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
    copy_reported: "Копирование отмечено владельцем",
    client_hashes_match: "Контрольные суммы клиента совпали",
    poweramp_owner_verified: "Poweramp проверен владельцем",
  };
  const snapshot = status?.export;
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
      <p>
        Подготовьте отдельный снимок готовой коллекции. Проверка сохраненных версий выполняется
        перед экспортом; последующие изменения не затронут этот снимок.
      </p>
      <button className="button primary" disabled={busy} onClick={onExport}>
        <FolderCheck size={18} />
        Подготовить файлы для переноса
      </button>
      {error && (
        <p role="alert" className="warning-text">
          {error}
        </p>
      )}
      {status && !snapshot && (
        <p className="muted small">
          Снимка пока нет. Сначала сохраните проверенные треки в готовую коллекцию, затем
          подготовьте файлы для переноса.
        </p>
      )}
      {snapshot && (
        <>
          <div className="delivery-path">
            <strong>
              {snapshot.files.length} файлов · {(snapshot.total_bytes / 1024 ** 2).toFixed(1)} МиБ
            </strong>
            <p className="muted small">
              Снимок от {new Date(snapshot.created_at).toLocaleString("ru-RU")}
            </p>
            <p>Каталог внутри хранилища приложения:</p>
            <code>state/exports/{snapshot.id}/</code>
          </div>
          <p>
            Скопируйте весь каталог снимка с сервера или скачайте файлы ниже. На телефоне сохраните{" "}
            <code>playlist.m3u8</code> рядом с папкой <code>files</code>, сохранив вложенные пути.
            Один плейлист не содержит музыку.
          </p>
          <div className="button-row">
            <a
              className="button secondary"
              href={`/api/exports/${snapshot.id}/playlist.m3u8`}
              download
            >
              Скачать плейлист
            </a>
            <a
              className="button subtle"
              href={`/api/exports/${snapshot.id}/manifest.json`}
              download
            >
              Контрольные суммы
            </a>
          </div>
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
            <label>
              Отчет SHA256, рассчитанный на устройстве
              <input
                type="file"
                accept="application/json,.json"
                onChange={(event) => void importFile(event.target.files?.[0])}
              />
            </label>
            <p className="muted small">
              Загружено контрольных сумм: {Object.keys(checksums).length}. Без отчета можно отметить
              только факт копирования.
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
            <button
              className="button secondary"
              disabled={saving || !device.trim()}
              onClick={() => void save()}
            >
              Сохранить результат проверки
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
