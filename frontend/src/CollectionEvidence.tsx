import { useEffect, useRef, useState } from "react";
import { api, messageFor, requestId } from "./api";

const groups = {
  timing: "Таймкоды",
  instrumental: "Возможные инструменталы",
  cover: "Обложки издания",
};
type Group = keyof typeof groups;
type Line = {
  line: number;
  reference_ms: number | null;
  matched_ms: number | null;
  delta_ms: number | null;
  words: number;
  matched_words: number;
};
type Item = {
  track_id: string;
  file: string;
  outcome: string;
  code?: string;
  timing?: { anchored?: number; lines?: number; offset_ms?: number | null; line_details?: Line[] };
};
type Report = {
  state: string;
  stage?: string;
  round?: number;
  retryable?: number;
  retry_at?: number;
  total?: number;
  completed?: number;
  failed?: number;
  counts: Partial<Record<Group, Record<string, number>>>;
  items: Item[];
};
const labels: Record<string, string> = {
  consistent: "Все строки совпадают в пределах 0,5 с",
  constant_offset: "Постоянный сдвиг времени",
  partial: "Подтверждена часть строк",
  no_anchors: "Ни одна строка не привязана",
  timing_disagreement: "Время строк расходится",
  checked_empty: "ASR не вернул слова",
  blocked: "Нет пригодной разметки",
  source_restricted: "Использование данных ограничено",
  reference_unavailable: "Нет доступного эталонного текста",
  asr_unavailable: "Нет расшифровки этой версии",
  identity_unknown: "Исполнение не установлено",
  identity_conflict: "Каталог указывает на другое исполнение",
  no_performance_evidence: "Каталог не подтвердил инструментальную версию",
  vocal_credit: "Каталог указывает вокалиста",
  instrumental_confirmed: "Инструментальная версия подтверждена",
  vocal_evidence: "Найден текст или вокал",
  front_found: "Найдена обложка подтверждённого издания",
  edition_unconfirmed: "Издание не установлено",
  no_front: "Нет лицевой обложки",
  cover_conflict: "Найдены разные обложки",
  field_locked: "Поле закреплено",
  source_unavailable: "Источник не ответил",
  source_disabled: "Внешний поиск выключен",
  edition_changed: "Издание изменилось",
  evidence_changed: "Данные изменились",
  candidate_limit: "Достигнут лимит вариантов",
  no_evidence: "Недостаточно данных",
};
const time = (ms: number | null) =>
  ms == null ? "—" : (ms / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 3 }) + " с";
export function CollectionEvidence({ id }: { id: string }) {
  const [group, setGroup] = useState<Group>("timing");
  const [offset, setOffset] = useState(0);
  const [report, setReport] = useState<Report | null>(null);
  const [cycle, refresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const applyKeys = useRef(new Map<string, string>());
  const base = "/preparation/" + encodeURIComponent(id) + "/evidence";
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const next = await api<Report>(base + "?group=" + group + "&offset=" + offset, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setReport(next);
        setError("");
        if (["running", "queued"].includes(next.state)) timer = setTimeout(() => void read(), 5000);
      } catch (e) {
        if (!controller.signal.aborted) setError(messageFor(e));
      }
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [base, group, offset, cycle]);
  async function act(action: "check" | "cover" | "retry" = "check") {
    setBusy(true);
    setError("");
    try {
      if (action === "cover") {
        const { tokens } = await api<{ tokens: Record<string, string> }>(base + "/covers");
        const ids = Object.keys(tokens);
        if (ids.length) {
          const body = JSON.stringify({
            kind: "apply",
            track_ids: ids,
            params: {
              acceptance_mode: "confirmed_fields",
              field_tokens: tokens,
              publish_confirmed: true,
            },
          });
          if (!applyKeys.current.has(body)) applyKeys.current.set(body, requestId());
          await api("/jobs", {
            method: "POST",
            headers: { "Idempotency-Key": applyKeys.current.get(body)! },
            body,
          });
          setReceipt(
            "На запись обложек отправлено: " + ids.length + ". Результат доступен в обработке.",
          );
        } else setReceipt("Подтверждённых обложек для записи сейчас нет.");
      } else if (action === "retry") {
        await api(base + "/retry?expected_round=" + (report?.round ?? 0), { method: "POST" });
        setReceipt("Повторная проверка поставлена в очередь.");
      } else await api(base, { method: "POST" });
      refresh((v) => v + 1);
    } catch (e) {
      setError(messageFor(e));
    } finally {
      setBusy(false);
    }
  }
  const counts = report?.counts[group] || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return (
    <section className="card" aria-label="Проверка недостающих данных">
      <h3>Проверка недостающих данных</h3>
      {(!report || report.state === "idle") && (
        <p>
          Сверьте строки с аудио, проверьте инструментальные версии и найдите обложки подтверждённых
          изданий.
        </p>
      )}
      {report?.state === "idle" && (
        <button className="button primary" disabled={busy} onClick={() => void act()}>
          Проверить недостающие данные
        </button>
      )}
      {report && report.state !== "idle" && (
        <>
          <p>
            Проверено файлов: {(report.completed || 0) + (report.failed || 0)} из {report.total}.
            Ошибок: {report.failed || 0}.
          </p>
          {report.stage === "waiting_provider" && !!report.retry_at && (
            <p>
              Источник попросил паузу. Проверка продолжится после{" "}
              {new Date(report.retry_at * 1000).toLocaleTimeString("ru-RU")}.
            </p>
          )}
          {!!report.retryable && (
            <button className="button secondary" disabled={busy} onClick={() => void act("retry")}>
              Повторить незавершённые проверки ({report.retryable})
            </button>
          )}
          <div className="button-row">
            {(Object.keys(groups) as Group[]).map((g) => (
              <button
                key={g}
                className={"button " + (g === group ? "primary" : "secondary")}
                aria-pressed={g === group}
                onClick={() => {
                  setOffset(0);
                  setGroup(g);
                }}
              >
                {groups[g]}
              </button>
            ))}
          </div>
          <p>
            {Object.entries(counts)
              .map(([k, n]) => (labels[k] || k) + " · " + n)
              .join(" / ") || "Пока нет результатов"}
          </p>
          {group === "cover" &&
            !!counts.front_found &&
            ["succeeded", "completed_with_errors"].includes(report.state) && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void act("cover")}
              >
                Записать подтверждённые обложки
              </button>
            )}
          {report.items.map((item) => (
            <details key={item.track_id}>
              <summary>
                {item.file} · {labels[item.outcome] || item.outcome}
              </summary>
              {item.code && <p>{item.code}</p>}
              {item.timing?.line_details && (
                <>
                  <p>
                    Сопоставлено строк: {item.timing.anchored} из {item.timing.lines}. Медианный
                    сдвиг: {time(item.timing.offset_ms ?? null)}.
                  </p>
                  <table>
                    <thead>
                      <tr>
                        <th>Строка</th>
                        <th>Слов совпало</th>
                        <th>В тексте</th>
                        <th>В аудио</th>
                        <th>Разница</th>
                      </tr>
                    </thead>
                    <tbody>
                      {item.timing.line_details.map((l) => (
                        <tr key={l.line}>
                          <td>{l.line}</td>
                          <td>
                            {l.matched_words} / {l.words}
                          </td>
                          <td>{time(l.reference_ms)}</td>
                          <td>{time(l.matched_ms)}</td>
                          <td>{time(l.delta_ms)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </details>
          ))}
          <div className="button-row">
            {offset > 0 && (
              <button className="button secondary" onClick={() => setOffset((v) => v - 20)}>
                Предыдущие
              </button>
            )}
            {offset + 20 < total && (
              <button className="button secondary" onClick={() => setOffset((v) => v + 20)}>
                Следующие
              </button>
            )}
          </div>
        </>
      )}
      {receipt && <p role="status">{receipt}</p>}
      {error && (
        <p role="alert">
          {error} <button onClick={() => refresh((v) => v + 1)}>Обновить</button>
        </p>
      )}
    </section>
  );
}
