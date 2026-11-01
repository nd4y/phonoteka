import { useEffect, useState } from "react";
import { Clock3, FileStack, Pin, PinOff, RefreshCw, Save, ShieldCheck } from "lucide-react";
import { api, messageFor } from "./api";
import "./RetentionPanel.css";

type Policy = { version: number; keep_all: boolean; min_age_days: number; keep_last: number };
type PinSet = { version: number; items: Record<string, { reason: string; created_at: string }> };
type Lease = {
  id: string;
  export_id: string;
  purpose: string;
  created_at: number;
  expires_at: number;
};
type Info = { policy: Policy; pins: PinSet; active_export_leases: Lease[] };
type Artifact = {
  id: string;
  kind: "history" | "export";
  identity: string;
  label: string;
  family: string;
  created_at: string;
  protected_reasons: string[];
  eligible: boolean;
  bytes: number;
};
type Preview = {
  objects: Artifact[];
  total_objects: number;
  page: number;
  eligible_count: number;
  eligible_bytes: number;
  unreconciled_journals: string[];
  fully_verified: boolean;
};
const reasons: Record<string, string> = {
  keep_all_policy: "Политика: сохранять всё",
  keep_last: "Последняя сохраняемая версия",
  last_readable_rollback: "Последняя версия с проверенным хешем",
  minimum_age: "Не достигнут минимальный возраст",
  owner_pin: "Закреплено вручную",
  active_job_reference: "Используется задачей",
  unreconciled_publication_intent: "Не завершена сверка операции Ready",
  recovery_required: "Требуется сверка после восстановления",
  unfinished_retention_journal: "Не завершена предыдущая очистка",
  active_export_lease: "Защищена передача на устройство",
  latest_export: "Текущее поколение экспорта",
  artifact_missing_or_unsafe: "Артефакт недоступен или небезопасен",
  unfinished_or_mismatched_journal: "Журнал не согласован",
  journal_missing: "Нет журнала версии",
  backup_integrity_mismatch: "Хеш резервной версии не совпадает",
  export_integrity_mismatch: "Повреждён снимок экспорта",
};
const bytes = (value: number) =>
  value >= 1024 ** 3
    ? (value / 1024 ** 3).toFixed(2) + " ГиБ"
    : value >= 1024 ** 2
      ? (value / 1024 ** 2).toFixed(1) + " МиБ"
      : Math.round(value / 1024) + " КиБ";

export default function RetentionPanel({
  onDirtyChange,
}: {
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [info, setInfo] = useState<Info | null>(null),
    [form, setForm] = useState<Policy | null>(null),
    [preview, setPreview] = useState<Preview | null>(null);
  const [page, setPage] = useState(1),
    [generation, setGeneration] = useState(0),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [dirty, setDirty] = useState(false),
    [error, setError] = useState("");
  const [pinReason, setPinReason] = useState("Сохранить выбранную версию для отката");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([
      api<Info>("/retention", { signal: controller.signal }),
      api<Preview>("/retention/preview?page=" + page + "&page_size=20", {
        signal: controller.signal,
      }),
    ])
      .then(([data, plan]) => {
        setInfo(data);
        setForm(data.policy);
        setPreview(plan);
        setDirty(false);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(messageFor(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [page, generation]);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  async function action(callback: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await callback();
      setGeneration((value) => value + 1);
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }
  const disabled = loading || busy;
  return (
    <section className="panel retention-panel" aria-label="Хранение резервных версий и экспортов">
      <div className="panel-heading">
        <div>
          <h2>Версии и свободное место</h2>
          <p className="muted small">
            Последние rollback-версии, закрепления и активные передачи защищены. Автоматической
            очистки нет.
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Обновить состояние хранения"
          disabled={disabled || dirty}
          onClick={() => setGeneration((value) => value + 1)}
        >
          <RefreshCw size={18} className={loading ? "spin" : ""} />
        </button>
      </div>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {form && (
        <div className="retention-policy">
          <label>
            <input
              type="checkbox"
              checked={form.keep_all}
              disabled={disabled}
              onChange={(event) => {
                setForm({ ...form, keep_all: event.target.checked });
                setDirty(true);
              }}
            />
            Сохранять все резервные версии и экспорты
          </label>
          <div className="retention-policy-fields">
            <label>
              Минимальный возраст, дней
              <input
                type="number"
                min={1}
                max={36500}
                value={form.min_age_days}
                disabled={disabled}
                onChange={(event) => {
                  setForm({ ...form, min_age_days: Number(event.target.value) });
                  setDirty(true);
                }}
              />
            </label>
            <label>
              Минимум версий каждого файла
              <input
                type="number"
                min={1}
                max={10000}
                value={form.keep_last}
                disabled={disabled}
                onChange={(event) => {
                  setForm({ ...form, keep_last: Number(event.target.value) });
                  setDirty(true);
                }}
              />
            </label>
          </div>
          <button
            className="button secondary"
            disabled={disabled || !dirty || form.min_age_days < 1 || form.keep_last < 1}
            onClick={() =>
              void action(() =>
                api("/retention/policy", {
                  method: "PUT",
                  body: JSON.stringify({
                    base_version: info!.policy.version,
                    keep_all: form.keep_all,
                    min_age_days: form.min_age_days,
                    keep_last: form.keep_last,
                  }),
                }),
              )
            }
          >
            <Save size={16} />
            Сохранить политику хранения
          </button>
          {dirty && (
            <p className="warning-text small">
              Изменение политики ещё не сохранено. Оно определяет кандидатов для явной очистки
              оператором.
            </p>
          )}
        </div>
      )}
      {preview && (
        <>
          <div className="retention-summary">
            <ShieldCheck size={18} />
            <span>
              {preview.eligible_count
                ? "Предварительных кандидатов: " +
                  preview.eligible_count +
                  " (" +
                  bytes(preview.eligible_bytes) +
                  ")"
                : "Кандидатов для очистки по текущей политике нет"}
            </span>
          </div>
          <p className="retention-hint muted small">
            В этом списке SHA-256 ещё не перепроверены. CLI проверяет полный состав и хеши перед
            формированием применимого плана. Оригинал, work и Ready не удаляются.
          </p>
          {preview.unreconciled_journals.length > 0 && (
            <p className="notice warning">
              Незавершённых журналов очистки: {preview.unreconciled_journals.length}. Перед новой
              очисткой оператор должен выполнить reconcile.
            </p>
          )}
          <label className="retention-pin-reason">
            Причина закрепления версии
            <input
              value={pinReason}
              maxLength={500}
              disabled={disabled || dirty}
              onChange={(event) => setPinReason(event.target.value)}
            />
          </label>
          <div className="retention-artifacts">
            {preview.objects.map((item) => (
              <article key={item.id}>
                <div className="retention-artifact-heading">
                  <FileStack size={17} />
                  <div>
                    <strong>{item.label}</strong>
                    <small>
                      {item.kind === "export"
                        ? "Снимок экспорта"
                        : item.family === "ready"
                          ? "Откат Ready"
                          : "Откат рабочей версии"}{" "}
                      · {bytes(item.bytes)} ·{" "}
                      {new Date(item.created_at).toLocaleDateString("ru-RU")}
                    </small>
                  </div>
                  <span className={"badge " + (item.eligible ? "warm" : "purple")}>
                    {item.eligible ? "Кандидат для полной сверки" : "Защищено"}
                  </span>
                </div>
                <p className="retention-reasons">
                  {item.protected_reasons.map((reason) => reasons[reason] || reason).join(" · ") ||
                    "Окончательное решение принимает оператор по полному проверенному отчёту."}
                </p>
                {info?.pins.items[item.id] && (
                  <p className="muted small">
                    Причина закрепления: {info.pins.items[item.id].reason}
                  </p>
                )}
                <div className="button-row">
                  <button
                    className="button subtle"
                    disabled={disabled || dirty || pinReason.trim().length < 3}
                    onClick={() =>
                      void action(() =>
                        api("/retention/pins", {
                          method: "PUT",
                          body: JSON.stringify({
                            base_version: info!.pins.version,
                            kind: item.kind,
                            identity: item.identity,
                            pinned: !info!.pins.items[item.id],
                            reason: pinReason,
                          }),
                        }),
                      )
                    }
                  >
                    {info?.pins.items[item.id] ? <PinOff size={15} /> : <Pin size={15} />}
                    {info?.pins.items[item.id] ? "Снять закрепление" : "Закрепить версию"}
                  </button>
                  {item.kind === "export" && (
                    <button
                      className="button subtle"
                      disabled={disabled || dirty}
                      onClick={() =>
                        void action(() =>
                          api("/export-leases", {
                            method: "POST",
                            body: JSON.stringify({ export_id: item.identity }),
                          }),
                        )
                      }
                    >
                      <Clock3 size={15} />
                      Защитить передачу на 30 минут
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
          {preview.total_objects > 20 && (
            <div className="retention-pagination">
              <button
                className="button secondary"
                disabled={disabled || dirty || page === 1}
                onClick={() => setPage((value) => value - 1)}
              >
                Назад
              </button>
              <span className="muted small">
                {page} из {Math.ceil(preview.total_objects / 20)}
              </span>
              <button
                className="button secondary"
                disabled={disabled || dirty || page * 20 >= preview.total_objects}
                onClick={() => setPage((value) => value + 1)}
              >
                Далее
              </button>
            </div>
          )}
        </>
      )}
      {!!info?.active_export_leases.length && (
        <div className="retention-leases">
          <h3>Защита активных передач</h3>
          {info.active_export_leases.map((lease) => (
            <div key={lease.id}>
              <p>
                Экспорт {lease.export_id.slice(0, 12)} ·{" "}
                {lease.purpose === "download" ? "Активная HTTP-загрузка" : "Передача на устройство"}
                <small>TTL до {new Date(lease.expires_at * 1000).toLocaleString("ru-RU")}</small>
              </p>
              {lease.purpose === "delivery" && (
                <div className="button-row">
                  <button
                    className="button subtle"
                    disabled={disabled || dirty}
                    onClick={() =>
                      void action(() =>
                        api("/export-leases/" + lease.id + "/renew", { method: "POST" }),
                      )
                    }
                  >
                    Продлить на 30 минут
                  </button>
                  <button
                    className="button subtle"
                    disabled={disabled || dirty}
                    onClick={() =>
                      void action(() => api("/export-leases/" + lease.id, { method: "DELETE" }))
                    }
                  >
                    Завершить защиту
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <details className="retention-cli">
        <summary>Явная очистка оператором</summary>
        <p>
          Остановите worker этого экземпляра и откройте контейнер обслуживания с той же проверенной
          конфигурацией. Создайте и прочитайте весь отчёт:
        </p>
        <pre>python -m phonoteka.retention preview --output /tmp/retention-plan.json</pre>
        <p>После проверки всех кандидатов оператор явно применяет конкретный plan_id:</p>
        <pre>
          python -m phonoteka.retention apply --plan /tmp/retention-plan.json --approval-token
          PLAN_HASH
        </pre>
        <p>
          Хеш подтверждает выбранный отчёт, право доступа предоставляет локальный оператор.
          Политика, ссылки, закрепления и файлы перепроверяются под блокировками. После прерывания
          очистки:
        </p>
        <pre>python -m phonoteka.retention reconcile</pre>
        <p>
          Команда сверки не удаляет новые файлы. Недостаток места не снимает защиту последней
          версии.
        </p>
      </details>
    </section>
  );
}
