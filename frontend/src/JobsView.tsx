import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDown,
  CheckCheck,
  ChevronDown,
  CircleAlert,
  Clock3,
  FileAudio,
  LoaderCircle,
  Pause,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { api, messageFor } from "./api";
import {
  etaText,
  isActiveJob,
  jobNames,
  jobStates,
  jobTone,
  needsJobAuthorization,
  jobErrorMessage,
} from "./jobs";
import { emptyActivityFeed, subscribeJobActivity } from "./jobActivityFeed";
import {
  JobAuthorizationPanel,
  reauthorizationMessage,
  reauthorizationRequest,
} from "./JobAuthorization";
import type { ActivityEvent, LiveJob } from "./jobs";
import type { Job } from "./types";
import "./JobsView.css";
import { useBackLayer } from "./useAppHistory";
import { ProcessingStages } from "./ProcessingStages";

type Props = { jobs: Job[]; refresh: () => Promise<void>; onCancel: (id: string) => Promise<void> };
const dateOf = (value: string) =>
  new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
const timeOf = (value: string) =>
  new Date(value).toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
const count = (value: number) => value.toLocaleString("ru-RU");
function rememberedJob() {
  try {
    return sessionStorage.getItem("phonoteka-job-selection") || "";
  } catch {
    return "";
  }
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div className="notice error" role="alert">
      <CircleAlert size={18} aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function JobResults({ job }: { job: Job }) {
  const [detail, setDetail] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function load() {
    setLoading(true);
    setError("");
    try {
      const result = await api<Job>(`/jobs/${encodeURIComponent(job.id)}`);
      if (mounted.current) setDetail(result);
    } catch (err) {
      if (mounted.current) setError(messageFor(err));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }
  return (
    <details
      className="job-results"
      onToggle={(event) => {
        if (event.currentTarget.open) void load();
      }}
    >
      <summary>Результаты по файлам</summary>
      {loading ? (
        <span className="loading">
          <LoaderCircle className="spin" size={16} />
          Загрузка результатов
        </span>
      ) : error ? (
        <ErrorNotice message={error} />
      ) : detail?.results?.length ? (
        <>
          <p className="muted small">
            Результаты отдельных попыток. Наличие результата не означает, что все теги найдены и
            подтверждены.
          </p>
          <pre className="diagnostic">{JSON.stringify(detail.results, null, 2)}</pre>
          <button className="button subtle" onClick={() => void load()}>
            <RefreshCw size={15} />
            Обновить результаты
          </button>
        </>
      ) : (
        <p className="muted small">Результатов по отдельным файлам пока нет.</p>
      )}
    </details>
  );
}

function ActivityLog({ events }: { events: ActivityEvent[] }) {
  const ref = useRef<HTMLOListElement>(null);
  const follows = useRef(true);
  const seen = useRef(0);
  const [unread, setUnread] = useState(0);
  useLayoutEffect(() => {
    if (follows.current && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
      seen.current = events.at(-1)?.id || 0;
      setUnread(0);
    } else setUnread(events.filter((event) => event.id > seen.current).length);
  }, [events]);
  function scroll() {
    const node = ref.current;
    if (!node) return;
    follows.current = node.scrollHeight - node.scrollTop - node.clientHeight < 32;
    if (follows.current) {
      seen.current = events.at(-1)?.id || 0;
      setUnread(0);
    }
  }
  return (
    <div className="job-log-wrap">
      <ol
        className="job-event-log"
        role="log"
        aria-label="События задачи"
        aria-live="off"
        tabIndex={0}
        ref={ref}
        onScroll={scroll}
      >
        {events.length ? (
          events.map((event) => (
            <li
              key={event.id}
              className={
                event.state === "waiting_authorization"
                  ? "event-authorization"
                  : ["failed", "conflict", "needs_review", "interrupted"].includes(
                        event.state || "",
                      )
                    ? "event-error"
                    : ""
              }
            >
              <time dateTime={event.created_at} title={dateOf(event.created_at)}>
                {timeOf(event.created_at)}
              </time>
              <div>
                <span>{event.message}</span>
                {event.filename && <strong title={event.filename}>{event.filename}</strong>}
                {event.code && <small>Код: {event.code}</small>}
                {event.retry_at && (
                  <small>
                    Повтор после {dateOf(new Date(event.retry_at * 1000).toISOString())}
                  </small>
                )}
              </div>
            </li>
          ))
        ) : (
          <li className="job-log-empty">Событий пока нет. Они появятся при запуске задачи.</li>
        )}
      </ol>
      {!!unread && (
        <button
          className="button secondary job-log-latest"
          onClick={() => {
            follows.current = true;
            seen.current = events.at(-1)?.id || 0;
            setUnread(0);
            if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
          }}
        >
          <ArrowDown size={15} />
          Новые события: {unread}
        </button>
      )}
      <p className="muted small job-log-note">
        Последние 200 событий. Если прокрутить вверх, новые строки не сдвигают чтение.
      </p>
    </div>
  );
}

export function JobsView({ jobs, refresh, onCancel }: Props) {
  const [selectedId, setSelectedId] = useState(
    () =>
      jobs.find((job) => job.id === rememberedJob())?.id ||
      jobs.find(isActiveJob)?.id ||
      jobs[0]?.id ||
      "",
  );
  const [manualActivity, setManualActivity] = useState(false);
  useBackLayer(manualActivity && !!selectedId, () => {
    setSelectedId("");
    setManualActivity(false);
  });
  const [feedState, setFeedState] = useState(() => emptyActivityFeed(selectedId));
  const { activity, events, connection, error } =
    feedState.jobId === selectedId ? feedState : emptyActivityFeed(selectedId);
  const [controlError, setControlError] = useState("");
  const [pending, setPending] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const refreshSelected = useRef<(() => Promise<void>) | null>(null);
  const selectionInitialized = useRef(jobs.length > 0);
  const currentRefresh = useRef(refresh);
  useEffect(() => {
    currentRefresh.current = refresh;
  }, [refresh]);
  useEffect(() => {
    if (selectedId) {
      try {
        sessionStorage.setItem("phonoteka-job-selection", selectedId);
      } catch {
        /* Optional browser storage. */
      }
    }
  }, [selectedId]);
  useEffect(() => {
    if (!selectionInitialized.current && jobs.length) {
      selectionInitialized.current = true;
      setSelectedId(
        jobs.find((job) => job.id === rememberedJob())?.id ||
          jobs.find(isActiveJob)?.id ||
          jobs[0].id,
      );
    }
  }, [jobs]);

  useEffect(() => {
    if (!selectedId) return;
    const feed = subscribeJobActivity(selectedId, setFeedState, () => {
      void currentRefresh.current().catch(() => undefined);
    });
    refreshSelected.current = feed.refresh;
    return () => {
      feed.stop();
      refreshSelected.current = null;
    };
  }, [selectedId]);

  const control = useCallback(
    async (job: Job, action: "pause" | "resume" | "cancel" | "retry-storage") => {
      setPending(job.id);
      setControlError("");
      try {
        if (action === "cancel") await onCancel(job.id);
        else await api(`/jobs/${encodeURIComponent(job.id)}/${action}`, { method: "POST" });
        await refresh();
        if (selectedId === job.id) await refreshSelected.current?.();
      } catch (err) {
        setControlError(reauthorizationMessage(err));
      } finally {
        setPending("");
      }
    },
    [onCancel, refresh, selectedId],
  );
  async function reauthorize(job: Job, scopes: string[]) {
    const request = reauthorizationRequest(job, scopes);
    if (!request) return;
    setPending(job.id);
    setControlError("");
    try {
      await api(`/jobs/${encodeURIComponent(job.id)}/reauthorize`, request);
      await refresh();
      if (selectedId === job.id) await refreshSelected.current?.();
    } catch (err) {
      setControlError(reauthorizationMessage(err));
    } finally {
      setPending("");
    }
  }
  async function reload() {
    setRefreshing(true);
    setControlError("");
    try {
      await refresh();
      await refreshSelected.current?.();
    } catch (err) {
      setControlError(messageFor(err));
    } finally {
      setRefreshing(false);
    }
  }
  const activeCount = jobs.filter(isActiveJob).length;
  return (
    <section className="panel jobs-panel jobs-live-panel">
      <div className="panel-heading">
        <div>
          <h2>Очередь обработки</h2>
          <p className="muted small">
            {activeCount ? `Активных задач: ${count(activeCount)}. ` : ""}Прогресс обновляется
            автоматически, события поступают в реальном времени.
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Обновить очередь"
          disabled={refreshing}
          onClick={() => void reload()}
        >
          <RefreshCw className={refreshing ? "spin" : ""} size={18} />
        </button>
      </div>
      <p className="job-attempts-note muted small">
        Процент показывает обработанные попытки. Найденные теги и результаты распознавания
        проверяйте в карточках файлов.
      </p>
      {controlError && (
        <div className="job-control-error">
          <ErrorNotice message={controlError} />
        </div>
      )}
      {!jobs.length ? (
        <div className="empty">
          <div className="icon-surface">
            <Activity size={30} />
          </div>
          <h3>Здесь пока тихо</h3>
          <p>После запуска сканирования или обработки здесь появятся задачи, прогресс и события.</p>
        </div>
      ) : (
        <div className="jobs-list">
          {jobs.map((original) => {
            const job = (activity?.job.id === original.id ? activity.job : original) as LiveJob;
            const active = isActiveJob(job);
            const tone = jobTone(job);
            const expanded = selectedId === job.id;
            const authorizationWaiting = needsJobAuthorization(job);
            const processed = job.processed ?? Math.min(job.total, job.completed + job.failed);
            const percent =
              job.total > 0
                ? Math.max(0, Math.min(100, Math.round((processed / job.total) * 100)))
                : 0;
            const Icon =
              tone === "cancelled"
                ? X
                : tone === "authorization"
                  ? ShieldCheck
                  : tone === "error"
                    ? CircleAlert
                    : tone === "paused"
                      ? Pause
                      : tone === "success"
                        ? CheckCheck
                        : tone === "waiting"
                          ? Clock3
                          : LoaderCircle;
            return (
              <article
                className={`job-card job-card--${tone} ${expanded ? "job-expanded" : ""}`}
                key={job.id}
              >
                <div className="job-top">
                  <div className="job-icon">
                    <Icon
                      className={
                        job.state === "running" &&
                        !authorizationWaiting &&
                        !job.pause_requested &&
                        !job.cancel_requested
                          ? "spin"
                          : ""
                      }
                      size={22}
                      aria-hidden="true"
                    />
                  </div>
                  <div className="job-title">
                    <h3>{jobNames[job.kind] || job.kind}</h3>
                    <time className="muted small" dateTime={job.created_at}>
                      {dateOf(job.created_at)}
                    </time>
                  </div>
                  <span className="badge job-status">
                    {job.cancel_requested && active
                      ? "Отмена запрошена"
                      : authorizationWaiting
                        ? jobStates.waiting_authorization
                        : active &&
                            jobStates[job.stage] &&
                            ["waiting_provider", "waiting_storage"].includes(job.stage)
                          ? jobStates[job.stage]
                          : jobStates[job.state] || job.state}
                  </span>
                  <div className="job-controls">
                    {active && job.stage === "waiting_storage" && (
                      <button
                        className="button subtle"
                        disabled={pending === job.id || job.cancel_requested}
                        onClick={() => void control(job, "retry-storage")}
                      >
                        <RefreshCw size={15} />
                        Проверить место
                      </button>
                    )}
                    {active &&
                      !authorizationWaiting &&
                      ![
                        "scan",
                        "export_ready",
                        "replaygain_album",
                        "release_audit",
                        "publish_sidecars",
                      ].includes(job.kind) && (
                        <button
                          className="button subtle"
                          disabled={
                            job.cancel_requested ||
                            (!!job.pause_requested && job.state !== "paused") ||
                            pending === job.id
                          }
                          onClick={() =>
                            void control(job, job.state === "paused" ? "resume" : "pause")
                          }
                        >
                          {job.state === "paused" ? <Play size={15} /> : <Pause size={15} />}
                          {job.state === "paused"
                            ? "Продолжить"
                            : job.pause_requested
                              ? "Ожидаем паузу"
                              : "Пауза"}
                        </button>
                      )}
                    {active && (
                      <button
                        className="button subtle"
                        disabled={job.cancel_requested || pending === job.id}
                        onClick={() => void control(job, "cancel")}
                      >
                        <Square size={14} />
                        Отменить
                      </button>
                    )}
                  </div>
                </div>
                <div className="job-progress-heading">
                  <strong className="job-percent">
                    {job.total ? `${percent}%` : active ? "-" : "0%"}
                  </strong>
                  <div>
                    <span>
                      {job.total
                        ? `${count(processed)} из ${count(job.total)} попыток`
                        : active
                          ? "Определяем объем работы"
                          : "Файлов для обработки нет"}
                    </span>
                    <small>
                      {count(job.completed)} успешно · {count(job.failed)} с ошибками
                    </small>
                  </div>
                  {active && (
                    <span className="job-eta">
                      <Clock3 size={14} aria-hidden="true" />
                      {etaText(job)}
                    </span>
                  )}
                </div>
                <progress
                  max={100}
                  value={job.total || !active ? percent : undefined}
                  aria-label={`Прогресс: ${jobNames[job.kind] || job.kind}`}
                  aria-valuetext={
                    job.total
                      ? `${percent}%, ${processed} из ${job.total} попыток`
                      : "Объем определяется"
                  }
                />
                <div className="job-current-action">
                  <Activity size={15} aria-hidden="true" />
                  <span>
                    {job.current_action ||
                      jobStates[job.stage] ||
                      jobStates[job.state] ||
                      "Подготовка"}
                    {job.active_count ? ` · файлов одновременно: ${job.active_count}` : ""}
                  </span>
                </div>
                {active && !authorizationWaiting && !!job.retry_at && (
                  <p className="muted small job-retry">
                    Автоматический повтор после{" "}
                    {dateOf(new Date(job.retry_at * 1000).toISOString())}. Выполненные попытки
                    сохраняются.
                  </p>
                )}
                {authorizationWaiting && (
                  <JobAuthorizationPanel
                    key={`${job.id}:${job.authorization?.version}`}
                    job={job}
                    pending={pending === job.id}
                    onGrant={(scopes) => {
                      void reauthorize(job, scopes);
                    }}
                  />
                )}
                {jobErrorMessage(job) &&
                  !authorizationWaiting &&
                  (job.error === "partial_scan" ? (
                    <p className="muted small job-retry">
                      Сканирование завершилось не полностью. Обработанные записи сохранены.
                    </p>
                  ) : (
                    <ErrorNotice message={jobErrorMessage(job)} />
                  ))}
                {job.cancel_requested && (
                  <p className="muted small job-retry">
                    Уже выполненные изменения сохраняются. Текущая операция с файлом завершается
                    безопасно.
                  </p>
                )}
                <button
                  className="button subtle job-activity-toggle"
                  aria-expanded={expanded}
                  aria-controls={`job-activity-${job.id}`}
                  onClick={() => {
                    setSelectedId(expanded ? "" : job.id);
                    setManualActivity(!expanded);
                  }}
                >
                  <ChevronDown className={expanded ? "is-expanded" : ""} size={17} />
                  {expanded ? "Скрыть действия и события" : "Действия и события"}
                </button>
                {expanded && (
                  <div className="job-activity" id={`job-activity-${job.id}`}>
                    {activity?.job.id === job.id && <ProcessingStages activity={activity} />}
                    <div className="job-activity-heading">
                      <h4>Текущие действия</h4>
                      <span className={`job-connection connection-${connection}`} role="status">
                        {connection === "live" ? (
                          <Wifi size={14} />
                        ) : connection === "offline" ? (
                          <WifiOff size={14} />
                        ) : (
                          <LoaderCircle className="spin" size={14} />
                        )}
                        {connection === "live"
                          ? "В реальном времени"
                          : connection === "offline"
                            ? "Нет сети"
                            : connection === "connecting"
                              ? "Подключение"
                              : "Переподключение"}
                      </span>
                    </div>
                    {error && <ErrorNotice message={error} />}
                    {!activity && !error ? (
                      <p className="muted small">Загружаем состояние задачи...</p>
                    ) : activity?.active_items.length ? (
                      <ul className="job-active-files">
                        {activity.active_items.map((item, index) => (
                          <li key={`${item.track_id || item.filename}:${index}`}>
                            <FileAudio size={17} aria-hidden="true" />
                            <div>
                              <strong title={item.filename}>{item.filename}</strong>
                              <small>
                                {item.action}
                                {item.started_at ? ` · с ${timeOf(item.started_at)}` : ""}
                              </small>
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted small job-no-active">
                        {active
                          ? job.current_action || "Ожидаем следующую операцию"
                          : "Активных операций нет"}
                      </p>
                    )}
                    {activity?.active_items_truncated && (
                      <p className="muted small">Показаны первые 100 активных файлов.</p>
                    )}
                    <details className="workflow-job-details">
                      <summary>Журнал событий ({events.length})</summary>
                      <ActivityLog key={job.id} events={events} />
                    </details>
                  </div>
                )}
                <JobResults job={job} />
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
