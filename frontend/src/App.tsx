import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Activity,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  AudioLines,
  BookOpen,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleHelp,
  Clock3,
  Disc3,
  FolderCheck,
  FolderSearch,
  Library,
  ListMusic,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Moon,
  Music2,
  Plus,
  RefreshCw,
  Save,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  Trash2,
  WandSparkles,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api, ApiError, messageFor, setSession } from "./api";
import { OperationsPanel } from "./OperationsPanel";
import { ProvidersPanel } from "./ProvidersPanel";
import { AutomationPanel } from "./AutomationPanel";
import { RecordingTypePanel, recordingLabel, recordingPanelKey } from "./RecordingTypePanel";
import { SchedulePanel } from "./SchedulePanel";
import { DeliveryPanel } from "./DeliveryPanel";
import { ReportView } from "./ReportView";
import { WorkflowOverview } from "./WorkflowOverview";
import { awaitReviewWrite, nextReviewId } from "./workflow";
import { GettingStarted, HelpPage } from "./GettingStarted";
import { clearGuideState, readGuideState, saveGuideState } from "./guideState";
import type { GuideStep } from "./guideState";
import type { CollectionFilter } from "./CollectionOverview";
import { AnalysisPanel } from "./AnalysisPanel";
import { YouTubePanel } from "./YouTubePanel";
import { CueView } from "./CueView";
import { AudioPlayer } from "./AudioPlayer";
import { PublishedTrackSearch, TrackFilters } from "./TrackFilters";
import AudioAuditSummary from "./AudioAuditSummary";
import PwaStatus from "./PwaStatus";
import { createPwaReloadGuard } from "./pwaReloadGuard";
import { acceptDetailNavigation, loadLatestResult, loadOrderedResult } from "./detailNavigation";
import { useOwnerActivity } from "./ownerActivity";
import { JobsView } from "./JobsView";
import { clearView, readView } from "./viewState";
import { AppBackContext, useAppHistory, useBackLayer } from "./useAppHistory";
import { navigationKey } from "./appHistory";
import type { Page, ViewState } from "./viewState";
import { useViewPersistence } from "./useViewPersistence";
import {
  clearPlaybackSession,
  playbackSessionFor,
  readPlaybackSession,
  restorePlaybackSession,
  savePlaybackSession,
} from "./playbackSession";
import TimingReview from "./TimingReview";
import { TrackPolicyPanel } from "./TrackPolicyPanel";
import { SourcePriorityPanel } from "./SourcePriorityPanel";
import LyricsReviewPanel from "./LyricsReviewPanel";
import { ProviderPolicyPanel } from "./ProviderPolicyPanel";
import PublicationPanel from "./PublicationPanel";
import { SidecarPublicationPanel } from "./SidecarPublicationPanel";
import { WebhooksPanel } from "./WebhooksPanel";
import RetentionPanel from "./RetentionPanel";
import { jobNames } from "./jobs";
import { changedTags, editorBaseline, editorDraftChanged, lyricsDraftChanged } from "./edits";
import { formatTime, parseLyrics, parseTime, serializeLyrics, validateLyrics } from "./lyrics";
import type { LyricLine } from "./lyrics";
import { editDocument } from "./lyrics-document";
import LyricsDocumentEditor from "./LyricsDocumentEditor";
import LyricsImport from "./LyricsImport";
import MediaSlotsPanel from "./MediaSlotsPanel";
import type { SlotSelection } from "./MediaSlotsPanel";
import ArtworkImport from "./ArtworkImport";
import type { ArtworkReceipt } from "./ArtworkImport";
import { reconcileImportedLines } from "./lyrics-document";
import type { Job, Session, Summary, Tags, Track, TrackPage, CueTrack } from "./types";

type Playback = {
  track: Track;
  cue?: CueTrack;
  autoPlay: boolean;
  initialPosition?: number;
  restored?: boolean;
  search: string;
  filter: string;
  serial: number;
};
type Neighbors = {
  catalog_position: number | null;
  previous_id: string | null;
  next_id: string | null;
  position: number | null;
  total: number;
};
const navigation: Array<{ id: Page; title: string; icon: LucideIcon }> = [
  { id: "reports", title: "Обзор", icon: BookOpen },
  { id: "library", title: "Коллекция", icon: Library },
  { id: "jobs", title: "Обработка", icon: Activity },
  { id: "ready", title: "Готовые файлы", icon: FolderCheck },
];
const states: Record<string, string> = {
  paused: "Приостановлено",
  waiting_provider: "Пауза внешнего источника",
  deferred: "Ожидает повтора",
  queued: "В очереди",
  running: "В работе",
  completed: "Завершено",
  completed_with_errors: "Завершено с ошибками",
  succeeded: "Завершено",
  failed: "Ошибка",
  canceled: "Отменено",
  cancelled: "Отменено",
  cancelled_partial: "Отменено частично",
  interrupted: "Прервано",
  needs_review: "Нужно решение",
  pending: "Ожидание",
  partial: "Частично",
  idle: "Ожидает работу",
  online: "В сети",
  offline: "Нет связи",
  ready: "Готово",
  ok: "Проверено",
  indexed: "В каталоге",
  accepted: "Принято",
  missing: "Файл недоступен",
  conflict: "Конфликт версий",
  error: "Ошибка",
  scanned: "В каталоге",
  unsupported: "Только чтение",
};
const activeStates = [
  "queued",
  "running",
  "pending",
  "paused",
  "waiting_provider",
  "waiting_authorization",
  "deferred",
];
const fields = [
  { key: "title", title: "Название" },
  { key: "artist", title: "Исполнитель" },
  { key: "album", title: "Альбом" },
  { key: "date", title: "Год / дата" },
  { key: "tracknumber", title: "Номер трека" },
];
const textTag = (tags: Tags | undefined, key: string) => {
  const value = tags?.[key];
  return Array.isArray(value) ? value.join("; ") : value == null ? "" : String(value);
};
const titleOf = (track: Track) =>
  textTag(track.tags, "title") ||
  track.filename ||
  track.relative_path.split("/").pop() ||
  "Без названия";
const durationOf = (value?: number) =>
  value && Number.isFinite(value)
    ? `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`
    : "-";
const dateOf = (value?: string | null) =>
  value
    ? new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })
    : "Нет данных";
const countOf = (value?: number) => (value == null ? "-" : value.toLocaleString("ru-RU"));

function Spinner({ label = "Загрузка" }: { label?: string }) {
  return (
    <span className="loading">
      <LoaderCircle className="spin" size={20} aria-hidden="true" />
      {label}
    </span>
  );
}
function ErrorBox({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div className="notice error" role="alert">
      <CircleAlert size={20} aria-hidden="true" />
      <div>{message}</div>
      {retry && (
        <button className="text-button" onClick={retry}>
          Повторить
        </button>
      )}
    </div>
  );
}
function Badge({ children, tone = "" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
function Cover({
  track,
  large = false,
  preview,
}: {
  track: Track;
  large?: boolean;
  preview?: string;
}) {
  const [failed, setFailed] = useState(false);
  const source =
    preview ||
    (track.has_art ? `/api/tracks/${encodeURIComponent(track.id)}/art?v=${track.sha256}` : "");
  useEffect(() => setFailed(false), [source]);
  return (
    <div className={`cover ${large ? "large" : ""}`}>
      {source && !failed ? (
        <img src={source} alt={`Обложка: ${titleOf(track)}`} onError={() => setFailed(true)} />
      ) : (
        <Disc3 size={large ? 56 : 24} strokeWidth={1.4} aria-label="Нет обложки" />
      )}
    </div>
  );
}

function Login({ onLogin, initialError }: { onLogin: () => Promise<void>; initialError?: string }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError || "");
  useEffect(() => {
    if (initialError) setError(initialError);
  }, [initialError]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      setPassword("");
      await onLogin();
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-layout">
      <div className="login-story">
        <div className="brand">
          <span className="brand-mark">
            <AudioLines />
          </span>
          Фонотека
        </div>
        <div>
          <div className="eyebrow">ВАША МУЗЫКА. В ПОРЯДКЕ.</div>
          <h1>
            У каждой песни
            <br />
            свое место.
          </h1>
          <p>
            Метаданные, обложки и слова, которые звучат вовремя. Ваша коллекция под вашим контролем.
          </p>
          <div className="record-illustration" aria-hidden="true">
            <div className="record">
              <div />
            </div>
            <span className="record-label">
              <Music2 size={19} />
              Личная коллекция
            </span>
          </div>
        </div>
        <span className="story-foot">
          <ShieldCheck size={16} />
          Один владелец. Собственное хранилище.
        </span>
      </div>
      <main className="login-main">
        <form className="login-card" onSubmit={submit}>
          <div className="icon-surface">
            <LockKeyhole size={27} />
          </div>
          <h2>С возвращением</h2>
          <p className="muted">Войдите, чтобы открыть свою фонотеку.</p>
          {error && <ErrorBox message={error} />}
          <label>
            Имя владельца
            <input
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              autoFocus
            />
          </label>
          <label>
            Пароль
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          <button
            className="button primary wide"
            type="submit"
            disabled={busy || !username || !password}
          >
            {busy ? (
              <Spinner label="Входим" />
            ) : (
              <>
                Открыть фонотеку
                <ArrowRight size={18} />
              </>
            )}
          </button>
          <p className="login-note">
            Доступ к этому экземпляру есть только у владельца. Учетная запись настраивается при
            установке.
          </p>
        </form>
        <span className="muted small">Фонотека · первая рабочая версия</span>
      </main>
    </div>
  );
}

export default function App() {
  const initialView = useRef(readView()).current;
  const pwaReloadGuard = useRef(createPwaReloadGuard()).current;
  const [pwaActivating, setPwaActivating] = useState(false);
  const initialPlayback = useRef(readPlaybackSession()).current;
  const savedPlayback = useRef(initialPlayback);
  const [session, updateSession] = useState<Session | null>(null);
  const [sessionError, setSessionError] = useState("");
  const [page, setPage] = useState<Page>(initialView.page);
  const [guideStep, setGuideStep] = useState<GuideStep>(
    initialView.guideStep || readGuideState().step,
  );
  useEffect(() => {
    if (session?.authenticated) saveGuideState({ step: guideStep, history: [] });
  }, [guideStep, session?.authenticated]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [tracks, setTracks] = useState<TrackPage | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [search, setSearch] = useState(initialView.query);
  const [query, setQuery] = useState(initialView.query);
  const [filter, setFilter] = useState(initialView.filter);
  const [pageNumber, setPageNumber] = useState(initialView.pageNumber);
  const [selected, setSelected] = useState<Track | null>(null);
  const [selectedId, setSelectedId] = useState(initialView.selectedId);
  const [dirty, setDirty] = useState(false);
  const [policyDirty, setPolicyDirty] = useState(false);
  const [showReports, setShowReports] = useState(false);
  const pendingReviewWrites = useRef(
    new Map<string, { job: Job; nextId: string | null; nextPage: number }>(),
  );
  const [reviewMoving, setReviewMoving] = useState(false);
  const reviewLock = useRef(false);
  const [reviewDirty, setReviewDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("phonoteka-theme") || "system";
    } catch {
      return "system";
    }
  });
  const [playback, setPlayback] = useState<Playback | null>(null);
  const playbackRequests = useRef(0);
  const authenticated = useRef(false);
  authenticated.current = !!session?.authenticated;
  const ownerActivity = useOwnerActivity(
    session,
    () => authenticated.current,
    (value) => {
      setSession(value);
      updateSession(value);
    },
  );
  const currentPlayback = useRef(playback);
  currentPlayback.current = playback;
  const playbackPosition = useRef(initialPlayback?.position || 0);
  const lastPlaybackWrite = useRef(0);
  const audioReady = useRef(false);
  const [restoringPlayback, setRestoringPlayback] = useState(!!initialPlayback);
  const [audioTime, setAudioTime] = useState(0);
  const [audioSupported, setAudioSupported] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);
  const [neighbors, setNeighbors] = useState<Neighbors | null>(null);
  const [playerNavigating, setPlayerNavigating] = useState(false);
  const navigationLock = useRef(false);
  const [restoringDetail, setRestoringDetail] = useState(
    !!initialView.selectedId && ["review", "lyrics"].includes(initialView.page),
  );
  const saveCurrentView = useViewPersistence(
    { page, query: search, filter, pageNumber, selectedId, guideStep },
    initialView,
    !!tracks && !loading && !detailLoading && !restoringDetail && !restoringPlayback,
    !!session?.authenticated,
  );
  const navigationHistory = useAppHistory({
    authenticated: !!session?.authenticated,
    view: { page, query: search, filter, pageNumber, selectedId, guideStep },
    capture: saveCurrentView.capture,
    approve: (target) =>
      (["review", "lyrics"].includes(page) &&
        ["review", "lyrics"].includes(target.page) &&
        selectedId === target.selectedId) ||
      clearDirty(),
    apply: restoreNavigation,
  });
  useBackLayer(
    detailLoading,
    () => {
      ++detailSequence.current;
      setDetailLoading(false);
      setRestoringDetail(false);
    },
    10,
    navigationHistory.layers,
  );
  function resetNavigation() {
    ++requestSequence.current;
    statusSequence.current.committed = ++statusSequence.current.issued;
    ++detailSequence.current;
    setLoading(false);
    setDetailLoading(false);
    setRestoringDetail(false);
    navigationHistory.clear();
    clearView();
    clearGuideState();
    setGuideStep("folder");
    setPage("reports");
    setSearch("");
    setQuery("");
    setFilter("all");
    setPageNumber(1);
    setSelectedId("");
    setSelected(null);
    setTracks(null);
    setSummary(null);
    setJobs([]);
    setDirty(false);
    setPolicyDirty(false);
    setReviewDirty(false);
    setSettingsDirty(false);
  }
  function restoreNavigation(target: ViewState) {
    setGuideStep(target.guideStep || "folder");
    const keepEditor =
      selected?.id === target.selectedId &&
      ["review", "lyrics"].includes(page) &&
      ["review", "lyrics"].includes(target.page);
    ++detailSequence.current;
    setRestoringDetail(false);
    setDetailLoading(false);
    setError("");
    if (query !== target.query || filter !== target.filter || pageNumber !== target.pageNumber) {
      ++requestSequence.current;
      setTracks(null);
    }
    setPage(target.page);
    setSearch(target.query);
    setQuery(target.query);
    setFilter(target.filter);
    setPageNumber(target.pageNumber);
    setSelectedId(target.selectedId);
    saveCurrentView.restore(target);
    if (!keepEditor) {
      setSelected(null);
      setDirty(false);
      setPolicyDirty(false);
      setReviewDirty(false);
      setSettingsDirty(false);
      if (target.selectedId && ["review", "lyrics"].includes(target.page)) {
        setDetailLoading(true);
        void loadLatestResult(
          detailSequence,
          () => api<Track>(`/tracks/${encodeURIComponent(target.selectedId)}`),
          (result) => setSelected(result),
          (problem) => {
            setSelectedId("");
            setError(messageFor(problem));
          },
          () => setDetailLoading(false),
        );
      }
    }
  }
  function recordCatalogStep(patch: Partial<ViewState>, group?: string) {
    const before = saveCurrentView.capture();
    const pending = detailLoading || restoringDetail;
    const currentId = pending && !selected ? "" : selectedId;
    // A filter/search action supersedes a pending card request. Its late reply
    // must not replace this newer action or overwrite its history snapshot.
    if (pending) {
      acceptDetailNavigation(detailSequence, () => true);
      setDetailLoading(false);
      setRestoringDetail(false);
      setSelectedId(currentId);
    }
    navigationHistory.push({ ...before, ...patch, selectedId: currentId }, group, before);
  }
  function changeSearch(value: string) {
    recordCatalogStep({ query: value, pageNumber: 1, scroll: 0, queueScroll: 0 }, "search");
    setSearch(value);
    setPageNumber(1);
  }
  function changeFilter(value: string) {
    recordCatalogStep({ filter: value, pageNumber: 1, scroll: 0, queueScroll: 0 });
    setFilter(value);
    setPageNumber(1);
  }
  function changePageNumber(value: number) {
    recordCatalogStep({ pageNumber: value, scroll: 0, queueScroll: 0 });
    setPageNumber(value);
  }
  function saveCurrentPlayback() {
    const value = currentPlayback.current;
    if (!authenticated.current || !value) return;
    const position =
      audioReady.current && audioRef.current
        ? audioRef.current.currentTime
        : playbackPosition.current;
    const saved = playbackSessionFor(value.track, position, value, value.cue);
    if (saved) {
      savePlaybackSession(saved);
      lastPlaybackWrite.current = Date.now();
    }
  }
  function rememberPlaybackPosition(seconds: number) {
    playbackPosition.current = seconds;
    setAudioTime(seconds);
    if (Date.now() - lastPlaybackWrite.current >= 1000) saveCurrentPlayback();
  }
  function forgetPlayback() {
    ++playbackRequests.current;
    ++detailSequence.current;
    savedPlayback.current = null;
    currentPlayback.current = null;
    clearPlaybackSession();
    audioRef.current?.pause();
    audioReady.current = false;
    setRestoringPlayback(false);
    setRestoringDetail(false);
    setDetailLoading(false);
    setPlayback(null);
    setAudioSupported(false);
    setAudioTime(0);
  }
  useEffect(() => {
    if (!playback) return;
    playbackPosition.current = playback.initialPosition ?? playback.cue?.start_seconds ?? 0;
    saveCurrentPlayback();
  }, [playback]);
  useEffect(() => {
    const remember = () => saveCurrentPlayback();
    window.addEventListener("pagehide", remember);
    return () => window.removeEventListener("pagehide", remember);
  }, []);
  useEffect(() => {
    setNeighbors(null);
    if (!playback || playback.cue) return;
    let disposed = false;
    void api<Neighbors>(
      `/tracks/${encodeURIComponent(playback.track.id)}/neighbors?search=${encodeURIComponent(playback.search)}&filter=${encodeURIComponent(playback.filter)}`,
    )
      .then((value) => {
        if (!disposed) {
          setNeighbors(value);
          if (
            !playback.restored &&
            value.catalog_position &&
            playback.search === query &&
            playback.filter === filter &&
            selectedId === playback.track.id
          )
            setPageNumber(Math.ceil(value.catalog_position / 30));
        }
      })
      .catch((err) => {
        if (!disposed) setError(messageFor(err));
      });
    return () => {
      disposed = true;
    };
  }, [
    playback?.track.id,
    playback?.track.sha256,
    playback?.cue?.number,
    playback?.search,
    playback?.filter,
  ]);
  function selectPlayback(
    track: Track,
    autoPlay = false,
    cue?: CueTrack,
    context = { search: query, filter },
  ) {
    savedPlayback.current = null;
    setRestoringPlayback(false);
    audioReady.current = false;
    playbackPosition.current = cue?.start_seconds || 0;
    ++playbackRequests.current;
    setAudioSupported(false);
    setAudioTime(cue?.start_seconds || 0);
    setPlayback((previous) => ({
      track,
      cue,
      autoPlay,
      ...context,
      serial: (previous?.serial || 0) + 1,
    }));
  }
  const requestSequence = useRef(0);
  const detailSequence = useRef(0);
  const statusSequence = useRef({ issued: 0, committed: 0 });
  const refreshSession = useCallback(async () => {
    try {
      const value = await api<Session>("/auth/session");
      if (!value.authenticated) {
        authenticated.current = false;
        ownerActivity.stop();
        forgetPlayback();
        resetNavigation();
      }
      setSession(value);
      updateSession(value);
      setSessionError("");
    } catch (err) {
      setSessionError(messageFor(err));
    }
  }, []);
  useEffect(() => {
    void refreshSession();
  }, [refreshSession]);
  useEffect(() => {
    if (!session?.authenticated || !restoringPlayback) return;
    const saved = savedPlayback.current;
    if (!saved) {
      setRestoringPlayback(false);
      return;
    }
    const current = ++playbackRequests.current;
    let disposed = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    const isCurrent = () =>
      !disposed && authenticated.current && current === playbackRequests.current;
    void restorePlaybackSession(
      saved,
      (id) => api<Track>(`/tracks/${encodeURIComponent(id)}`, { signal: controller.signal }),
      isCurrent,
    )
      .then((value) => {
        if (!isCurrent()) return;
        savedPlayback.current = null;
        if (value) {
          audioReady.current = false;
          setAudioSupported(false);
          playbackPosition.current = value.initialPosition;
          setAudioTime(value.initialPosition);
          setPlayback((previous) => ({
            ...value,
            restored: true,
            serial: (previous?.serial || 0) + 1,
          }));
        } else {
          clearPlaybackSession();
          setNotice("Сохраненный трек или композиция CUE изменились. Откройте аудио заново.");
        }
      })
      .catch((err) => {
        if (!isCurrent()) return;
        if (err instanceof ApiError && [404, 410].includes(err.status)) {
          savedPlayback.current = null;
          clearPlaybackSession();
        }
        setError(
          err instanceof DOMException && err.name === "AbortError"
            ? "Плеер не успел восстановиться. Проверьте соединение и откройте аудио заново."
            : messageFor(err),
        );
      })
      .finally(() => {
        window.clearTimeout(timeout);
        if (!disposed) setRestoringPlayback(false);
      });
    return () => {
      disposed = true;
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [session?.authenticated, restoringPlayback]);
  useEffect(() => {
    if (!session?.authenticated || !restoringDetail) return;
    let disposed = false;
    const current = ++detailSequence.current;
    setDetailLoading(true);
    void api<Track>(`/tracks/${encodeURIComponent(initialView.selectedId)}`)
      .then((track) => {
        if (disposed || current !== detailSequence.current || !authenticated.current) return;
        setSelected(track);
      })
      .catch((err) => {
        if (!disposed && current === detailSequence.current && authenticated.current) {
          setSelectedId("");
          setError(messageFor(err));
        }
      })
      .finally(() => {
        if (!disposed && current === detailSequence.current && authenticated.current) {
          setRestoringDetail(false);
          setDetailLoading(false);
        }
      });
    return () => {
      disposed = true;
    };
  }, [session?.authenticated, restoringDetail, initialView]);
  useEffect(() => {
    const expired = () => {
      authenticated.current = false;
      ownerActivity.stop();
      forgetPlayback();
      resetNavigation();
      updateSession({ authenticated: false });
      setSession({ authenticated: false });
      setSessionError("Сессия завершилась. Войдите снова.");
    };
    window.addEventListener("session-expired", expired);
    return () => window.removeEventListener("session-expired", expired);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("phonoteka-theme", theme);
    } catch {
      /* Theme still works without storage. */
    }
  }, [theme]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (search !== query) {
        setQuery(search);
        setPageNumber(1);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [search, query]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) =>
      pwaReloadGuard.beforeUnload(event, dirty || policyDirty || reviewDirty || settingsDirty);
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty, policyDirty, reviewDirty, settingsDirty, pwaReloadGuard]);
  const loadTracks = useCallback(async () => {
    if (!session?.authenticated) return;
    setLoading(true);
    await loadLatestResult(
      requestSequence,
      () =>
        api<TrackPage>(
          `/tracks?search=${encodeURIComponent(query)}&page=${pageNumber}&page_size=30&filter=${encodeURIComponent(filter)}`,
        ),
      setTracks,
      (err) => setError(messageFor(err)),
      () => setLoading(false),
    );
  }, [session?.authenticated, query, pageNumber, filter]);
  const loadStatus = useCallback(async () => {
    if (!session?.authenticated) return;
    await loadOrderedResult(
      statusSequence.current,
      () =>
        Promise.allSettled([
          api<Summary>("/summary"),
          api<{ items: Job[] }>("/jobs?active_first=true"),
        ]),
      (results) => {
        if (results[0].status === "fulfilled") setSummary(results[0].value);
        else setError(messageFor(results[0].reason));
        if (results[1].status === "fulfilled") setJobs(results[1].value.items);
        else setError(messageFor(results[1].reason));
      },
      (err) => setError(messageFor(err)),
    );
  }, [session?.authenticated]);
  useEffect(() => {
    void loadTracks();
  }, [loadTracks]);
  useEffect(() => {
    void loadStatus();
    if (!session?.authenticated) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) {
        void loadStatus();
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [loadStatus, session?.authenticated]);
  const completedJobs = jobs
    .filter((job) => !activeStates.includes(job.state))
    .map((job) => `${job.id}:${job.state}`)
    .join(",");
  useEffect(() => {
    if (completedJobs) void loadTracks();
  }, [completedJobs, loadTracks]);
  const clearDirty = () =>
    !(dirty || policyDirty || reviewDirty || settingsDirty) ||
    window.confirm("Есть несохраненные изменения. Перейти и потерять этот черновик?");
  function navigate(next: Page) {
    if (
      !acceptDetailNavigation(
        detailSequence,
        () =>
          next === page ||
          (["review", "lyrics"].includes(page) && ["review", "lyrics"].includes(next)) ||
          clearDirty(),
      )
    )
      return;
    const editor = next === "review" || next === "lyrics";
    const target: ViewState = {
      ...saveCurrentView.capture(),
      page: next,
      selectedId: editor ? selectedId : "",
      filter: next === "ready" ? "published" : page === "ready" ? "all" : filter,
      pageNumber: next === "ready" || page === "ready" ? 1 : pageNumber,
      scroll: next === page ? window.scrollY : 0,
      queueScroll: 0,
    };
    navigationHistory.push(target);
    setDetailLoading(false);
    setRestoringDetail(false);
    if (next !== "settings") setSettingsDirty(false);
    setPage(next);
    setFilter(target.filter);
    setPageNumber(target.pageNumber);
    setError("");
    if (!editor) {
      setDirty(false);
      setPolicyDirty(false);
      setReviewDirty(false);
      setSelected(null);
      setSelectedId("");
    }
    if (next !== page) saveCurrentView.restore(target);
  }
  function browseCollection(
    nextFilter: CollectionFilter | "published" | "review",
    nextPage: "library" | "review" | "ready" = "library",
  ) {
    if (!acceptDetailNavigation(detailSequence, clearDirty)) return;
    const target: ViewState = {
      ...saveCurrentView.capture(),
      page: nextPage,
      query: "",
      filter: nextFilter,
      pageNumber: 1,
      selectedId: "",
      scroll: 0,
      queueScroll: 0,
    };
    navigationHistory.push(target);
    restoreNavigation(target);
  }
  async function openTrack(
    track: Pick<Track, "id">,
    target: Page = "review",
    options: {
      autoPlay?: boolean;
      context?: { search: string; filter: string };
      catalogPage?: number;
      confirmed?: boolean;
      preservePlayback?: boolean;
    } = {},
  ) {
    if (!options.confirmed && !clearDirty()) return false;
    navigationHistory.endGroup();
    setRestoringDetail(false);
    setDetailLoading(true);
    setError("");
    return loadLatestResult(
      detailSequence,
      () => api<Track>(`/tracks/${encodeURIComponent(track.id)}`),
      (result) => {
        const before = saveCurrentView.capture();
        const next = {
          ...before,
          page: target,
          selectedId: result.id,
          query: options.context?.search ?? before.query,
          filter: options.context?.filter ?? before.filter,
          pageNumber: options.catalogPage ?? before.pageNumber,
          scroll: 0,
          queueScroll: 0,
        };
        navigationHistory.push(next, undefined, before);
        if (navigationKey(before) !== navigationKey(next)) saveCurrentView.restore(next);
        if (options.context) {
          setSearch(options.context.search);
          setQuery(options.context.search);
          setFilter(options.context.filter);
        }
        if (options.catalogPage) setPageNumber(options.catalogPage);
        setPage(target);
        setSelectedId(result.id);
        setSelected(result);
        setDirty(false);
        setPolicyDirty(false);
        setReviewDirty(false);
        setSettingsDirty(false);
        if (
          options.autoPlay &&
          !options.preservePlayback &&
          result.format !== "cue" &&
          !result.input_error &&
          result.status !== "missing"
        ) {
          const context =
            options.context ??
            (tracks?.items.some((item) => item.id === result.id)
              ? { search: query, filter }
              : { search: "", filter: "all" });
          selectPlayback(result, !!options.autoPlay, undefined, context);
        }
      },
      (err) => setError(messageFor(err)),
      () => setDetailLoading(false),
    );
  }
  async function movePlayback(direction: -1 | 1, continuePlaying: boolean) {
    if (!playback || navigationLock.current) return;
    if (playback.cue) {
      const items = playback.track.cue!.tracks.filter((item) => item.available);
      const index = items.findIndex((item) => item.number === playback.cue!.number);
      const item = items[index + direction];
      if (item)
        selectPlayback(playback.track, continuePlaying, item, {
          search: playback.search,
          filter: playback.filter,
        });
      return;
    }
    const id = direction === -1 ? neighbors?.previous_id : neighbors?.next_id;
    if (!id) return;
    navigationLock.current = true;
    setPlayerNavigating(true);
    const request = ++playbackRequests.current;
    try {
      const track = await api<Track>(`/tracks/${encodeURIComponent(id)}`);
      if (authenticated.current && request === playbackRequests.current)
        selectPlayback(track, continuePlaying, undefined, {
          search: playback.search,
          filter: playback.filter,
        });
    } catch (problem) {
      if (authenticated.current && request === playbackRequests.current)
        setError(messageFor(problem));
    } finally {
      navigationLock.current = false;
      setPlayerNavigating(false);
    }
  }
  async function advanceReview(track: Track, apply = false) {
    if (reviewLock.current || busy || loading || !tracks || !clearDirty()) return;
    reviewLock.current = true;
    setReviewMoving(true);
    setError("");
    const generation = detailSequence.current;
    let appliedKey: string | null = null;
    try {
      // Review includes CUE and files with errors; the player's neighbor endpoint
      // intentionally excludes these and must not silently skip them here.
      let nextId = nextReviewId(
        tracks.items.map((item) => item.id),
        track.id,
      );
      let nextPage = pageNumber;
      if (
        !nextId &&
        tracks.items.some((item) => item.id === track.id) &&
        pageNumber * tracks.page_size < tracks.total
      ) {
        nextPage++;
        const nextTracks = await api<TrackPage>(
          `/tracks?search=${encodeURIComponent(query)}&filter=${encodeURIComponent(filter)}&page=${nextPage}&page_size=30`,
        );
        nextId = nextTracks.items[0]?.id || null;
      }
      if (!authenticated.current || generation !== detailSequence.current) return;
      if (apply) {
        const key = `${track.id}:${track.proposal?.id}`;
        const pending = pendingReviewWrites.current.get(key);
        const job =
          pending?.job ||
          (await submitJob("apply", [track.id], { proposal_id: track.proposal?.id }));
        if (!job) return;
        if (pending) {
          nextId = pending.nextId;
          nextPage = pending.nextPage;
        } else pendingReviewWrites.current.set(key, { job, nextId, nextPage });
        setNotice(
          "Записываем выбранные данные. Следующая карточка откроется после успешного завершения.",
        );
        const result = await awaitReviewWrite(
          job,
          (id) => api<Job>(`/jobs/${encodeURIComponent(id)}`),
          () => authenticated.current && generation === detailSequence.current,
        );
        if (result === "abandoned") return;
        if (result === "pending") {
          setNotice(
            "Запись еще не завершена. Проверьте задачу в обработке или нажмите «Записать и дальше», чтобы проверить ее результат. Повторная задача не создается.",
          );
          return;
        }
        if (result === "failed") {
          pendingReviewWrites.current.delete(key);
          setNotice("");
          setError(
            "Изменения не записаны полностью. Карточка сохранена; причина доступна в обработке.",
          );
          return;
        }
        appliedKey = key;
        if (nextId && nextPage !== pageNumber) {
          // Applying the last item of a filtered page can move the next item
          // onto that page. Keep the editor and the visible queue together.
          const currentPage = await api<TrackPage>(
            `/tracks?search=${encodeURIComponent(query)}&filter=${encodeURIComponent(filter)}&page=${pageNumber}&page_size=30`,
          );
          if (currentPage.items.some((item) => item.id === nextId)) nextPage = pageNumber;
        }
        setNotice(
          "Изменения записаны. Предыдущая версия доступна в истории; готовая коллекция сохраняется отдельно.",
        );
      }
      if (!authenticated.current || generation !== detailSequence.current) return;
      if (nextId) {
        const opened = await openTrack({ id: nextId }, "review", {
          catalogPage: nextPage,
          confirmed: true,
          preservePlayback: true,
        });
        if (opened && appliedKey) pendingReviewWrites.current.delete(appliedKey);
      } else {
        if (appliedKey) pendingReviewWrites.current.delete(appliedKey);
        navigate("library");
        setNotice(
          apply
            ? "Последняя запись списка сохранена. Результаты доступны в обработке."
            : "Вы дошли до конца этого списка. Пропущенные записи остаются в коллекции.",
        );
      }
    } catch (problem) {
      if (authenticated.current) setError(messageFor(problem));
    } finally {
      reviewLock.current = false;
      setReviewMoving(false);
    }
  }
  async function submitJob(kind: string, ids: string[] = [], params: Record<string, unknown> = {}) {
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>("/jobs", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ kind, track_ids: ids, params }),
      });
      setNotice(`${jobNames[kind] || kind}: задача добавлена в очередь.`);
      await loadStatus();
      return job;
    } catch (err) {
      setError(messageFor(err));
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function logout(all = false) {
    if (!acceptDetailNavigation(detailSequence, clearDirty)) return;
    ownerActivity.stop();
    setDetailLoading(false);
    setRestoringDetail(false);
    try {
      await api(all ? "/auth/logout-all" : "/auth/logout", { method: "POST" });
      authenticated.current = false;
      forgetPlayback();
      resetNavigation();
      setSession({ authenticated: false });
      updateSession({ authenticated: false });
      setSelected(null);
      setTracks(null);
      setSummary(null);
      setDirty(false);
      setPolicyDirty(false);
      setReviewDirty(false);
      setSettingsDirty(false);
    } catch (err) {
      if (authenticated.current) ownerActivity.resume();
      setError(messageFor(err));
    }
  }
  if (!session)
    return (
      <>
        <PwaStatus showInstall={false} />
        <main className="loading-page">
          <div className="brand">
            <AudioLines />
            Фонотека
          </div>
          {sessionError ? (
            <ErrorBox message={sessionError} retry={() => void refreshSession()} />
          ) : (
            <Spinner label="Подключаемся к фонотеке" />
          )}
        </main>
      </>
    );
  if (!session.authenticated)
    return (
      <>
        <PwaStatus />
        <Login onLogin={refreshSession} initialError={sessionError} />
      </>
    );
  const activeJobs = jobs.filter((job) => activeStates.includes(job.state));
  const isEditor = page === "review" || page === "lyrics";
  const cueItems = playback?.cue ? playback.track.cue!.tracks.filter((item) => item.available) : [];
  const cueIndex = cueItems.findIndex((item) => item.number === playback?.cue?.number);
  const previousAvailable = playback?.cue ? cueIndex > 0 : !!neighbors?.previous_id;
  const nextAvailable = playback?.cue
    ? cueIndex >= 0 && cueIndex < cueItems.length - 1
    : !!neighbors?.next_id;
  const sectionTitle =
    page === "review"
      ? "Проверка записи"
      : page === "lyrics"
        ? "Текст записи"
        : page === "settings"
          ? "Настройки"
          : page === "guide"
            ? "Мастер настройки"
            : page === "help"
              ? "Справка"
              : navigation.find((item) => item.id === page)?.title || "Фонотека";
  const headings: Record<Page, [string, string]> = {
    guide: [
      "От папки к готовой музыке",
      "Понятный маршрут, без автоматической записи сомнительных данных.",
    ],
    help: ["Все по шагам", "Ответы на основные вопросы и быстрый путь к результату."],
    reports: ["Моя музыка", "Что готово, что требует решения и что делать дальше."],
    library: ["Все, что звучит", "Ваша коллекция, по одному треку за раз."],
    review: ["Внимание к деталям", "Проверьте изменения перед записью в файл."],
    lyrics: ["Слова в нужный момент", "Построчная синхронизация с полным аудиофайлом."],
    jobs: ["Обработка", "Текущие действия, оставшееся время и результаты."],
    ready: ["Готовые файлы", "Сохраненные версии и перенос музыки на устройство."],
    settings: ["Под вашим контролем", "Состояние экземпляра и возможности этой версии."],
  };
  return (
    <AppBackContext.Provider value={navigationHistory.layers}>
      <div
        className={`app-shell ${playback ? "has-player" : ""} ${isEditor && selected ? "review-mode" : ""}`}
        inert={pwaActivating || undefined}
        aria-busy={pwaActivating || undefined}
      >
        <a className="skip-link" href="#main-content">
          Перейти к содержимому
        </a>
        <aside className="sidebar">
          <a
            className="brand"
            href="#"
            onClick={(event) => {
              event.preventDefault();
              navigate("reports");
            }}
          >
            <span className="brand-mark">
              <AudioLines size={25} />
            </span>
            <span>Фонотека</span>
          </a>
          <span className="sidebar-label">МОЯ БИБЛИОТЕКА</span>
          <nav aria-label="Основная навигация">
            {navigation.map((item) => (
              <button
                key={item.id}
                aria-label={item.title}
                onClick={() => navigate(item.id)}
                aria-current={
                  page === item.id || (isEditor && item.id === "library") ? "page" : undefined
                }
              >
                <item.icon size={21} />
                <span>{item.title}</span>
                {item.id === "jobs" && activeJobs.length > 0 && (
                  <span className="nav-count">{activeJobs.length}</span>
                )}
              </button>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <div className="sidebar-account">
              <div className="avatar">{(session.username || "В")[0].toUpperCase()}</div>
              <div>
                <strong>{session.username || "Владелец"}</strong>
                <span>Личный экземпляр</span>
              </div>
              <button
                className="icon-button"
                aria-label="Выйти"
                title="Выйти"
                onClick={() => void logout()}
              >
                <LogOut size={18} />
              </button>
            </div>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="topbar-leading">
              <button
                className="icon-button app-back"
                data-app-back
                aria-label="Назад в приложении"
                title="На шаг назад"
                disabled={!navigationHistory.canBack}
                onClick={navigationHistory.back}
              >
                <ArrowLeft size={21} />
              </button>
              <span className="breadcrumb">
                <span className="breadcrumb-home">Моя фонотека</span>
                <span className="breadcrumb-divider" aria-hidden="true">
                  /
                </span>
                {sectionTitle}
              </span>
            </div>
            <div className="topbar-actions">
              <button
                className="icon-button"
                aria-label="Настройки"
                title="Настройки"
                onClick={() => navigate("settings")}
              >
                <Settings2 size={21} />
              </button>
              <button
                className="icon-button"
                aria-label="Открыть справку"
                title="Справка и мастер настройки"
                onClick={() => navigate("help")}
              >
                <CircleHelp size={21} />
              </button>
              <Badge>{summary?.version || "Alpha"}</Badge>
              <button
                className="icon-button"
                title="Сменить тему"
                aria-label="Сменить светлую или темную тему"
                onClick={() =>
                  setTheme(
                    theme === "dark"
                      ? "light"
                      : theme === "light"
                        ? "dark"
                        : matchMedia("(prefers-color-scheme: dark)").matches
                          ? "light"
                          : "dark",
                  )
                }
              >
                {theme === "dark" ? <Sun size={20} /> : <Moon size={20} />}
              </button>
            </div>
          </header>
          <PwaStatus
            beforeUpdate={clearDirty}
            onBeforeActivate={() => {
              saveCurrentView.freezeForReload();
              setPwaActivating(true);
            }}
            onActivationFailed={() => {
              pwaReloadGuard.reset();
              setPwaActivating(false);
              saveCurrentView.releaseReload();
            }}
            onBeforeReload={() =>
              pwaReloadGuard.prepareReload(() => {
                saveCurrentPlayback();
                saveCurrentView();
              })
            }
          />
          <main id="main-content" tabIndex={-1}>
            <div className="page-heading">
              <div>
                <div className="eyebrow">
                  {page === "library" ? "МУЗЫКА, КОТОРУЮ ВЫ СОБРАЛИ" : sectionTitle.toUpperCase()}
                </div>
                <h1>{headings[page][0]}</h1>
                <p>{headings[page][1]}</p>
              </div>
              {page === "library" && (
                <button
                  className="button primary"
                  onClick={() => void submitJob("scan")}
                  disabled={busy || activeJobs.some((job) => job.kind === "scan")}
                >
                  <FolderSearch size={19} />
                  {activeJobs.some((job) => job.kind === "scan")
                    ? "Сканирование идет"
                    : "Сканировать коллекцию"}
                </button>
              )}
            </div>
            {error && (
              <ErrorBox
                message={error}
                retry={() => {
                  setError("");
                  void loadTracks();
                  void loadStatus();
                }}
              />
            )}
            {notice && (
              <div className="notice success" role="status">
                <Check size={20} />
                <span>{notice}</span>
                <button
                  className="icon-button"
                  aria-label="Закрыть сообщение"
                  onClick={() => setNotice("")}
                >
                  <X size={17} />
                </button>
              </div>
            )}
            {summary?.read_only && (
              <div className="notice warning">
                <LockKeyhole size={20} />
                <span>
                  Хранилище открыто для чтения. Запись и публикация недоступны. Причина и режим
                  показаны в настройках.
                </span>
              </div>
            )}
            {page === "guide" && (
              <GettingStarted
                step={guideStep}
                onStep={(next) => {
                  if (next === guideStep) return;
                  const before = saveCurrentView.capture();
                  const target = { ...before, guideStep: next };
                  navigationHistory.push(target, undefined, before);
                  saveCurrentView.restore(target);
                  setGuideStep(next);
                }}
                onBack={navigationHistory.back}
                canBack={navigationHistory.canBack}
                summary={summary}
                jobs={jobs}
                busy={busy}
                onNavigate={(next) =>
                  next === "review"
                    ? browseCollection("all", "review")
                    : next === "ready"
                      ? browseCollection("published", "ready")
                      : navigate(next)
                }
                onStart={submitJob}
              />
            )}
            {page === "help" && <HelpPage onNavigate={navigate} />}
            {page === "library" && (
              <>
                <details className="workflow-library-tools">
                  <summary>Действия с коллекцией</summary>
                  <div className="button-row">
                    <button className="button secondary" onClick={() => navigate("guide")}>
                      Подключение папки и поиск данных
                      <ArrowRight size={18} />
                    </button>
                    <button
                      className="button subtle"
                      disabled={
                        busy ||
                        !summary?.capabilities.metadata_search ||
                        activeJobs.some((job) => job.kind === "analyze")
                      }
                      onClick={() => void submitJob("analyze", [], { scope: "pending" })}
                    >
                      Повторить незавершенный поиск
                    </button>
                    <button
                      className="button subtle"
                      disabled={
                        busy ||
                        !summary?.capabilities.recognition ||
                        activeJobs.some((job) => job.kind === "recognize")
                      }
                      onClick={() => void submitJob("recognize", [], { scope: "pending" })}
                    >
                      Продолжить распознавание
                    </button>
                  </div>
                  <details>
                    <summary>Повторить поиск во всей коллекции</summary>
                    <p className="muted small">
                      Новые запросы ко всем записям. Результаты сохраняются как предложения.
                    </p>
                    <div className="button-row">
                      <button
                        className="button secondary"
                        disabled={
                          busy ||
                          !summary?.capabilities.metadata_search ||
                          activeJobs.some((job) => job.kind === "analyze")
                        }
                        onClick={() => void submitJob("analyze", [], { scope: "all" })}
                      >
                        Найти данные для всей коллекции
                      </button>
                      <button
                        className="button secondary"
                        disabled={
                          busy ||
                          !summary?.capabilities.recognition ||
                          activeJobs.some((job) => job.kind === "recognize")
                        }
                        onClick={() => void submitJob("recognize", [], { scope: "all" })}
                      >
                        Распознать всю коллекцию
                      </button>
                    </div>
                  </details>
                </details>
                <div className="workflow-chips" aria-label="Быстрый выбор записей">
                  {[
                    ["all", "Все записи"],
                    ["review", "Нужно проверить"],
                    ["missing_art", "Без обложки"],
                    ["missing_lyrics", "Без текста"],
                    ["audit_errors", "Ошибки"],
                  ].map(([key, label]) => (
                    <button
                      key={key}
                      aria-pressed={filter === key}
                      onClick={() => changeFilter(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <section className="panel collection-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Ваша музыка</h2>
                      <span className="muted small">
                        Теги, обложки и тексты вашей коллекции
                        {summary?.cue_sheets
                          ? ` · CUE: ${summary.cue_sheets} альбомов, ${summary.cue_tracks} композиций`
                          : ""}
                      </span>
                    </div>
                    <button
                      className="icon-button"
                      aria-label="Обновить коллекцию"
                      onClick={() => {
                        void loadTracks();
                        void loadStatus();
                      }}
                    >
                      <RefreshCw size={18} />
                    </button>
                  </div>
                  <TrackFilters
                    search={search}
                    onSearch={changeSearch}
                    filter={filter}
                    onFilter={changeFilter}
                  />
                  <TrackList
                    tracks={tracks}
                    loading={loading}
                    selectedId={selectedId}
                    onSelect={(track) => void openTrack(track)}
                  />
                  <Pagination
                    tracks={tracks}
                    page={pageNumber}
                    onPage={changePageNumber}
                    loading={loading}
                  />
                </section>
                <div className="quiet-note">
                  <ShieldCheck size={17} />
                  Изменения сначала сохраняются как предложение. Запись в файл запускается отдельным
                  действием.
                </div>
              </>
            )}
            {isEditor && (
              <div className={`editor-layout ${selected ? "has-selection" : ""}`}>
                <section className="panel track-queue">
                  <div className="panel-heading">
                    <h2>Треки</h2>
                    <span className="muted small">{countOf(tracks?.total)}</span>
                  </div>
                  <TrackFilters
                    compact
                    search={search}
                    onSearch={changeSearch}
                    filter={filter}
                    onFilter={changeFilter}
                  />
                  <div className="queue-tracks">
                    {tracks?.items.map((track) => (
                      <button
                        key={track.id}
                        className={`queue-track ${selectedId === track.id ? "selected" : ""}`}
                        onClick={() => void openTrack(track, page)}
                      >
                        <Cover track={track} />
                        <span>
                          <strong>{titleOf(track)}</strong>
                          <small>{textTag(track.tags, "artist") || "Исполнитель не указан"}</small>
                          <span className="queue-meta">
                            {track.format.toUpperCase()}
                            {recordingLabel(track) && ` · ${recordingLabel(track)}`}
                            {track.proposal?.input_sha256 === track.sha256 ? " · Предложение" : ""}
                          </span>
                        </span>
                        {track.proposal && (
                          <span className="proposal-dot" aria-label="Есть предложение" />
                        )}
                      </button>
                    ))}
                    {loading && <Spinner />}
                    {!loading && tracks?.items.length === 0 && (
                      <div className="empty compact">
                        <Search size={25} />
                        <p>Треков не найдено</p>
                      </div>
                    )}
                  </div>
                  <Pagination
                    compact
                    tracks={tracks}
                    page={pageNumber}
                    onPage={changePageNumber}
                    loading={loading}
                  />
                </section>
                <section className="editor-content" inert={detailLoading || undefined}>
                  {selected?.cue_sheets?.length ? (
                    <div className="notice info cue-links">
                      <Disc3 size={20} />
                      <div>
                        {selected.cue_album_image && (
                          <p>
                            Это альбомный образ. Изменение и публикация файла заблокированы, чтобы
                            сохранить все композиции.
                          </p>
                        )}
                        {selected.cue_sheets.map((sheet) => (
                          <button
                            className="button subtle"
                            key={sheet.id}
                            onClick={() => {
                              void openTrack({ id: sheet.id }, page);
                            }}
                          >
                            Открыть CUE: {sheet.title} ({sheet.track_count})
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  {detailLoading && !selected ? (
                    <div className="panel empty">
                      <Spinner label="Открываем карточку" />
                    </div>
                  ) : selected ? (
                    selected.format === "cue" ? (
                      <CueView
                        selectedNumber={
                          playback?.track.id === selected.id ? playback.cue?.number : undefined
                        }
                        onPlay={(item) => selectPlayback(selected, true, item)}
                        enabled={!!summary?.capabilities.cue_export}
                        busy={busy}
                        onExtract={() => void submitJob("extract_cue", [selected.id])}
                        key={`${selected.id}:${selected.sha256}:${selected.revision}`}
                        track={selected}
                        onBack={navigationHistory.back}
                        onRefresh={() => {
                          void openTrack(selected, page);
                        }}
                      />
                    ) : (
                      <>
                        <TrackEditor
                          onPlay={() => selectPlayback(selected, true)}
                          onSkip={() => void advanceReview(selected)}
                          onApplyNext={() => advanceReview(selected, true)}
                          moving={reviewMoving}
                          editingBlocked={policyDirty || reviewDirty}
                          audioRef={audioRef}
                          audioTime={audioTime}
                          audioSupported={
                            audioSupported && playback?.track.id === selected.id && !playback.cue
                          }
                          key={`${selected.id}:${selected.proposal?.id || ""}:${selected.sha256}:${selected.lyrics_document?.version || 0}`}
                          track={selected}
                          page={page}
                          summary={summary}
                          busy={busy}
                          onDirty={setDirty}
                          onTab={navigate}
                          onBack={navigationHistory.back}
                          onRefresh={() => {
                            void openTrack(selected, page);
                          }}
                          onClassified={(track) => {
                            setSelected((current) => (current?.id === track.id ? track : current));
                            setDirty(false);
                            void loadTracks();
                          }}
                          onSave={async (body) => {
                            await api(`/tracks/${encodeURIComponent(selected.id)}/proposal`, {
                              method: "POST",
                              headers: { "If-Match": `"${selected.sha256}"` },
                              body: JSON.stringify(body),
                            });
                            setDirty(false);
                            const result = await api<Track>(
                              `/tracks/${encodeURIComponent(selected.id)}`,
                            );
                            setSelected(result);
                            setNotice("Предложение сохранено. Файл пока не изменен.");
                            void loadTracks();
                          }}
                          onJob={async (kind, params) => {
                            const job = await submitJob(kind, [selected.id], params);
                            if (job) {
                              setNotice(
                                `${jobNames[kind]}: задача принята. Результат появится в разделе «Обработка».`,
                              );
                            }
                          }}
                        />
                        <details className="panel policy-details">
                          <summary>Закрепленные поля и источники</summary>
                          <TrackPolicyPanel
                            key={selected.id + selected.sha256}
                            track={selected}
                            disabled={busy || dirty || reviewDirty}
                            onDirtyChange={setPolicyDirty}
                            onChanged={() => {
                              void loadTracks();
                            }}
                          />
                        </details>
                        <details className="panel policy-details">
                          <summary>Приемка текста и профиль публикации</summary>
                          <LyricsReviewPanel
                            key={selected.id + selected.sha256}
                            track={selected}
                            disabled={busy || dirty || policyDirty}
                            onDirtyChange={setReviewDirty}
                            onSeek={
                              audioSupported && playback?.track.id === selected.id && !playback.cue
                                ? (seconds) => {
                                    if (audioRef.current) audioRef.current.currentTime = seconds;
                                  }
                                : undefined
                            }
                            onChanged={() => {
                              void loadTracks();
                            }}
                          />
                        </details>
                        <details className="panel policy-details">
                          <summary>Готовая версия и восстановление</summary>
                          <PublicationPanel
                            trackId={selected.id}
                            disabled={busy || dirty || policyDirty || reviewDirty || settingsDirty}
                            onJobQueued={() => {
                              void loadStatus();
                            }}
                          />
                          <SidecarPublicationPanel
                            ids={[selected.id]}
                            disabled={
                              busy ||
                              dirty ||
                              policyDirty ||
                              reviewDirty ||
                              !summary?.capabilities.publish
                            }
                            onQueued={() => {
                              void loadStatus();
                            }}
                          />
                        </details>
                      </>
                    )
                  ) : (
                    <div className="panel empty editor-empty">
                      <div className="icon-surface">
                        <SlidersHorizontal size={30} />
                      </div>
                      <h2>Выберите трек</h2>
                      <p>
                        Откройте карточку слева, чтобы проверить теги,
                        <br />
                        обложку и синхронизированный текст.
                      </p>
                    </div>
                  )}
                </section>
              </div>
            )}
            {page === "jobs" && (
              <JobsView
                jobs={jobs}
                refresh={loadStatus}
                onCancel={async (id) => {
                  await api(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
                  setNotice(
                    "Запрошена отмена. Текущая запись файла завершится безопасно. Уже завершенные изменения сохранятся.",
                  );
                }}
              />
            )}
            {page === "ready" && (
              <>
                <DeliveryPanel busy={busy} onExport={() => void submitJob("export_ready")} />
                <details className="panel policy-details">
                  <summary>Обложки и тексты рядом с аудио текущей страницы</summary>
                  <SidecarPublicationPanel
                    ids={tracks?.items.map((track) => track.id) || []}
                    disabled={busy || !summary?.capabilities.publish}
                    onQueued={() => {
                      void loadStatus();
                    }}
                  />
                </details>
                <details className="panel policy-details">
                  <summary>План сохранения и история версий</summary>
                  <PublicationPanel
                    disabled={busy}
                    onJobQueued={() => {
                      void loadStatus();
                    }}
                  />
                </details>
                <div className="notice info">
                  <FolderCheck size={21} />
                  <span>
                    Здесь показаны опубликованные версии. Передача на телефон и проверка в Poweramp
                    выполняются отдельно.
                  </span>
                </div>
                <section className="panel collection-panel">
                  <div className="panel-heading">
                    <h2>Текущие версии в готовой коллекции</h2>
                    <Badge>{countOf(tracks?.total)}</Badge>
                  </div>
                  <PublishedTrackSearch
                    search={search}
                    onSearch={changeSearch}
                    onPage={setPageNumber}
                  />
                  <TrackList
                    tracks={tracks}
                    loading={loading}
                    selectedId=""
                    onSelect={(track) => void openTrack(track)}
                    emptyTitle={
                      search.trim() ? "По запросу ничего не найдено" : "Готовых файлов пока нет"
                    }
                    emptyDescription={
                      tracks?.total
                        ? "Перейдите на предыдущую страницу."
                        : search.trim()
                          ? "Измените или очистите поисковый запрос."
                          : "Проверьте записи в «Коллекции», затем сохраните готовые версии через «План сохранения и история версий». После этого они появятся здесь."
                    }
                  />
                  <Pagination
                    tracks={tracks}
                    page={pageNumber}
                    onPage={changePageNumber}
                    loading={loading}
                  />
                </section>
              </>
            )}
            {page === "reports" && (
              <>
                <WorkflowOverview
                  jobs={jobs}
                  onAction={(action) => {
                    if (action === "review") browseCollection("review", "review");
                    else if (action === "errors") browseCollection("audit_errors");
                    else
                      navigate(action === "jobs" ? "jobs" : action === "ready" ? "ready" : "guide");
                  }}
                />
                <details
                  className="panel workflow-report-details"
                  onToggle={(event) => setShowReports(event.currentTarget.open)}
                >
                  <summary>Подробный отчет, альбомы и дубликаты</summary>
                  {showReports && (
                    <ReportView
                      onGuide={() => navigate("guide")}
                      onBrowse={browseCollection}
                      onPreview={(id, aligned, revisions) => {
                        const current = ++playbackRequests.current;
                        const position =
                          aligned &&
                          playback &&
                          !playback.cue &&
                          revisions[playback.track.id] === playback.track.sha256
                            ? audioRef.current?.currentTime || 0
                            : 0;
                        void api<Track>(`/tracks/${encodeURIComponent(id)}`)
                          .then((track) => {
                            if (current !== playbackRequests.current) return;
                            selectPlayback(track, true, undefined, { search: "", filter: "all" });
                            setPlayback((previous) =>
                              previous
                                ? {
                                    ...previous,
                                    initialPosition:
                                      revisions[track.id] === track.sha256 ? position : 0,
                                  }
                                : previous,
                            );
                          })
                          .catch((err) => {
                            if (current === playbackRequests.current) setError(messageFor(err));
                          });
                      }}
                      onJob={submitJob}
                      onOpen={(id) => {
                        void openTrack({ id });
                      }}
                    />
                  )}
                </details>
              </>
            )}
            {page === "settings" && (
              <SettingsView
                summary={summary}
                theme={theme}
                setTheme={setTheme}
                onDirtyChange={setSettingsDirty}
                onLogoutAll={() => void logout(true)}
              />
            )}
            <footer className="app-footer">
              <span>
                <ShieldCheck size={14} />
                Личная коллекция
              </span>
              <span>
                {summary?.version || "Подключение к серверу"} ·{" "}
                {summary?.worker?.state
                  ? `Обработчик: ${states[summary.worker.state] || summary.worker.state}`
                  : "Состояние обработчика неизвестно"}
              </span>
            </footer>
          </main>
        </div>
        {playback && (
          <AudioPlayer
            key={`${playback.track.id}:${playback.track.sha256}:${playback.cue?.number || 0}:${playback.serial}`}
            mediaRef={audioRef}
            src={
              playback.cue
                ? `/api/tracks/${encodeURIComponent(playback.track.id)}/cue/${playback.cue.number}/audio`
                : `/api/tracks/${encodeURIComponent(playback.track.id)}/audio?v=${playback.track.sha256}`
            }
            title={playback.cue?.title || titleOf(playback.track)}
            subtitle={
              playback.cue?.performer ||
              textTag(playback.track.tags, "artist") ||
              "Исполнитель не указан"
            }
            cover={
              playback.track.has_art
                ? `/api/tracks/${encodeURIComponent(playback.track.id)}/art?v=${playback.track.sha256}`
                : undefined
            }
            initialPosition={playback.initialPosition}
            duration={playback.track.duration}
            start={playback.cue?.start_seconds}
            end={playback.cue?.end_seconds}
            autoPlay={playback.autoPlay}
            onPosition={rememberPlaybackPosition}
            onReady={(ready) => {
              audioReady.current = ready;
              setAudioSupported(ready);
            }}
            navigationBusy={playerNavigating}
            navigationLabel={
              playback.cue
                ? `CUE · ${cueIndex + 1} из ${cueItems.length}`
                : neighbors?.position
                  ? `${neighbors.position} из ${neighbors.total} в списке`
                  : "Текущая версия файла"
            }
            onPrevious={
              previousAvailable ? (keepPlaying) => void movePlayback(-1, keepPlaying) : undefined
            }
            onNext={nextAvailable ? (keepPlaying) => void movePlayback(1, keepPlaying) : undefined}
            onOpen={() => {
              if (selectedId === playback.track.id && selected) navigate("review");
              else void openTrack(playback.track, "review", { preservePlayback: true });
            }}
            onClose={forgetPlayback}
          />
        )}
      </div>
    </AppBackContext.Provider>
  );
}

function TrackList({
  tracks,
  loading,
  selectedId,
  onSelect,
  emptyTitle = "Пока ничего не найдено",
  emptyDescription = "Измените фильтр или запустите сканирование рабочей коллекции.",
}: {
  tracks: TrackPage | null;
  loading: boolean;
  selectedId: string;
  onSelect: (track: Track) => void;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  return (
    <div className="track-list" aria-busy={loading}>
      <div className="track-table-head">
        <span>ТРЕК / ИСПОЛНИТЕЛЬ</span>
        <span>ФОРМАТ</span>
        <span>ТЕГИ</span>
        <span>ОБЛОЖКА</span>
        <span>ТЕКСТ</span>
        <span />
      </div>
      {!tracks ? (
        <div className="empty">
          <Spinner />
        </div>
      ) : tracks.items.length === 0 ? (
        <div className="empty">
          <div className="icon-surface">
            <Library size={30} />
          </div>
          <h3>{tracks.total ? "Нет треков на этой странице" : emptyTitle}</h3>
          <p>{emptyDescription}</p>
        </div>
      ) : (
        tracks.items.map((track) => (
          <button
            className={`track-row ${selectedId === track.id ? "selected" : ""}`}
            key={track.id}
            onClick={() => onSelect(track)}
          >
            <span className="track-name">
              <Cover track={track} />
              <span>
                <strong>{titleOf(track)}</strong>
                <small>
                  {textTag(track.tags, "artist") || "Исполнитель не указан"}
                  {textTag(track.tags, "album") && ` · ${textTag(track.tags, "album")}`}
                </small>
                <span className="track-mobile-meta">
                  {track.format.toUpperCase()} · {durationOf(track.duration)}
                  {recordingLabel(track) && ` · ${recordingLabel(track)}`}
                  {track.proposal?.input_sha256 === track.sha256 ? " · Предложение" : ""}
                </span>
              </span>
            </span>
            <span className="format-cell">
              {track.format.toUpperCase()}
              <small>{durationOf(track.duration)}</small>
              {recordingLabel(track) && (
                <small className={track.possible_mix ? "warning-text" : ""}>
                  {recordingLabel(track)}
                </small>
              )}
            </span>
            <span className="state-cell">
              <StatusMark
                ok={!!textTag(track.tags, "title") && !!textTag(track.tags, "artist")}
                label="Теги"
              />
            </span>
            <span className="state-cell">
              <StatusMark ok={track.has_art} label="Обложка" />
            </span>
            <span className="lyric-state">
              <Badge tone={track.lrc ? "good" : track.lyrics ? "warm" : ""}>
                {track.lrc ? "С таймкодами" : track.lyrics ? "Без таймкодов" : "Нет текста"}
              </Badge>
            </span>
            <ChevronRight className="row-arrow" size={18} />
          </button>
        ))
      )}
    </div>
  );
}
function StatusMark({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`status-mark ${ok ? "good" : ""}`}
      aria-label={`${label}: ${ok ? "есть" : "не указаны"}`}
    >
      {ok ? <Check size={18} /> : <span>-</span>}
    </span>
  );
}
function Pagination({
  tracks,
  page,
  onPage,
  loading,
  compact = false,
}: {
  tracks: TrackPage | null;
  page: number;
  onPage: (page: number) => void;
  loading: boolean;
  compact?: boolean;
}) {
  const pages = tracks ? Math.max(1, Math.ceil(tracks.total / tracks.page_size)) : 1;
  return (
    <div className={`pagination ${compact ? "compact" : ""}`}>
      <span className="muted small">
        {compact
          ? `${page} / ${pages}`
          : tracks
            ? `Показано ${tracks.items.length ? (page - 1) * tracks.page_size + 1 : 0}-${Math.min(page * tracks.page_size, tracks.total)} из ${countOf(tracks.total)}`
            : "Загрузка каталога"}
      </span>
      <div>
        <button
          className="icon-button"
          disabled={loading || page <= 1}
          onClick={() => onPage(page - 1)}
          aria-label="Предыдущая страница"
        >
          <ChevronLeft size={18} />
        </button>
        <button
          className="icon-button"
          disabled={loading || page >= pages}
          onClick={() => onPage(page + 1)}
          aria-label="Следующая страница"
        >
          <ChevronRight size={18} />
        </button>
      </div>
    </div>
  );
}

type EditorProps = {
  onPlay: () => void;
  onSkip: () => void;
  onApplyNext: () => Promise<void>;
  moving: boolean;
  editingBlocked?: boolean;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  audioTime: number;
  audioSupported: boolean;
  track: Track;
  page: Page;
  summary: Summary | null;
  busy: boolean;
  onDirty: (value: boolean) => void;
  onTab: (page: Page) => void;
  onBack: () => void;
  onRefresh: () => void;
  onClassified: (track: Track) => void;
  onSave: (body: Record<string, unknown>) => Promise<void>;
  onJob: (kind: string, params?: Record<string, unknown>) => Promise<void>;
};
function TrackEditor({
  onPlay,
  onSkip,
  onApplyNext,
  moving,
  editingBlocked = false,
  audioRef,
  audioTime,
  audioSupported,
  track,
  page,
  summary,
  busy,
  onDirty,
  onTab,
  onBack,
  onRefresh,
  onClassified,
  onSave,
  onJob,
}: EditorProps) {
  const activeProposal = track.proposal?.input_sha256 === track.sha256 ? track.proposal : null;
  const lyricDocument = track.lyrics_document;
  const [baseline] = useState(() =>
    editorBaseline(
      track,
      fields.map((field) => field.key),
    ),
  );
  const [lyricRows, setLyricRows] = useState(baseline.lines);
  const [lyricMetadata, setLyricMetadata] = useState(baseline.metadata);
  const [draft, setDraft] = useState(baseline.tags);
  const [lrc, setLrc] = useState(baseline.lrc);
  const [cover, setCover] = useState("");
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");
  const [slotSelection, setSlotSelection] = useState<SlotSelection | undefined>(
    baseline.slotSelection,
  );
  const [artworkReceipt, setArtworkReceipt] = useState<ArtworkReceipt | undefined>();
  const currentDraft = {
    tags: draft,
    lrc,
    lines: lyricRows,
    metadata: lyricMetadata,
    cover,
    slotSelection,
    artworkReceipt,
  };
  const lyricsTouched = lyricsDraftChanged(baseline, currentDraft);
  const changed = editorDraftChanged(baseline, currentDraft);
  const [classificationDirty, setClassificationDirty] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [showAllFields, setShowAllFields] = useState(false);
  useBackLayer(sourcesOpen, () => setSourcesOpen(false));
  useEffect(() => {
    onDirty(changed || classificationDirty);
  }, [changed, classificationDirty, onDirty]);
  const writable = track.can_write && !summary?.read_only;
  let hasUntimed = false;
  let lyricsInvalid = "";
  try {
    const parsed = parseLyrics(lrc);
    hasUntimed = parsed.lines.some((line) => line.time === null && line.text.trim());
    lyricsInvalid = validateLyrics(parsed.lines, track.duration) || "";
  } catch (err) {
    lyricsInvalid = messageFor(err);
  }
  async function save() {
    setSaving(true);
    setLocalError("");
    try {
      const tags = changedTags(track.tags, draft, activeProposal?.tags);
      const body: Record<string, unknown> = { tags };
      if (activeProposal) body.base_proposal_id = activeProposal.id;
      if (lyricsTouched || activeProposal?.lrc != null) {
        body.lrc = lrc;
        if (lyricDocument && lyricRows)
          body.lyrics_document = editDocument(lyricDocument.version, lyricRows, lyricMetadata);
      }
      if (cover) {
        body.cover = cover;
        if (artworkReceipt) {
          body.artwork_preview_id = artworkReceipt.preview_id;
          body.confirm_smaller_cover = artworkReceipt.confirm_smaller_cover;
        }
      }
      if (slotSelection) body.slot_selection = slotSelection;
      if (
        (cover && !slotSelection?.cover_slot_id) ||
        (lyricsTouched && !slotSelection?.lyrics_slot_id)
      )
        throw new Error("Выберите целевой встроенный слот ниже перед сохранением.");
      await onSave(body);
      onDirty(false);
    } catch (err) {
      setLocalError(messageFor(err));
    } finally {
      setSaving(false);
    }
  }
  async function changeLyricLock(line: LyricLine, kind: "words" | "timing") {
    if (!lyricDocument || changed) return;
    setSaving(true);
    setLocalError("");
    try {
      await api(`/tracks/${encodeURIComponent(track.id)}/lyrics-document/locks`, {
        method: "PUT",
        headers: { "If-Match": track.sha256 },
        body: JSON.stringify({
          base_version: lyricDocument.version,
          lines: [
            {
              line_id: line.id,
              words_locked: kind === "words" ? !line.wordsLocked : !!line.wordsLocked,
              timing_locked: kind === "timing" ? !line.timingLocked : !!line.timingLocked,
            },
          ],
        }),
      });
      onRefresh();
    } catch (err) {
      setLocalError(messageFor(err));
    } finally {
      setSaving(false);
    }
  }
  function acceptTiming(index: number, proposedMs: number) {
    try {
      const parsed = { lines: lyricRows || parseLyrics(lrc).lines };
      const reference = track.analysis?.alignment?.lines[index];
      const lineIndex = reference?.line_id
        ? parsed.lines.findIndex((item) => item.id === reference.line_id)
        : index;
      const line = parsed.lines[lineIndex];
      if (
        !reference ||
        !line ||
        reference.text.trim() !== line.text.trim() ||
        line.time !== reference.original_ms
      )
        throw new Error(
          "Эта строка черновика уже отличается от исходной. Сохраните предложение и повторите сопоставление.",
        );
      if (line.timingLocked)
        throw new Error("Время этой строки закреплено. Сначала снимите закрепление времени.");
      const next = parsed.lines.map((item, i) =>
        i === lineIndex ? { ...item, time: proposedMs } : item,
      );
      if (
        !Number.isFinite(proposedMs) ||
        proposedMs < 0 ||
        (track.duration && proposedMs > track.duration * 1000)
      )
        throw new Error("Предложенное время выходит за границы аудио.");
      setLyricRows(next);
      setLrc(serializeLyrics(next));
      setLocalError("");
    } catch (err) {
      setLocalError(messageFor(err));
    }
  }
  function suggestTimingShift(deltaMs: number) {
    try {
      const parsed = { lines: lyricRows || parseLyrics(lrc).lines };
      const original = track.proposal?.lrc ?? track.lrc ?? "";
      if (lrc !== original)
        throw new Error("Черновик уже изменен. Проверьте строки отдельно перед общим сдвигом.");
      const next = parsed.lines.map((line) => ({
        ...line,
        time: line.time === null || line.timingLocked ? line.time : line.time + deltaMs,
      }));
      const invalid = validateLyrics(next, track.duration);
      if (invalid) throw new Error(invalid);
      setLyricRows(next);
      setLrc(serializeLyrics(next));
      setLocalError("");
    } catch (err) {
      setLocalError(messageFor(err));
    }
  }
  const applyBlocked =
    !writable ||
    !summary?.capabilities.apply ||
    !activeProposal ||
    changed ||
    hasUntimed ||
    !!lyricsInvalid ||
    busy ||
    saving;
  return (
    <div inert={editingBlocked || moving || undefined}>
      <div inert={classificationDirty || undefined}>
        <div className="panel editor-panel">
          <div className="track-detail-heading">
            <button
              className="icon-button mobile-back"
              data-app-back
              onClick={onBack}
              aria-label="Назад к списку"
            >
              <ArrowLeft size={21} />
            </button>
            <Cover track={track} large preview={cover} />
            <div className="track-detail-title">
              <span className="eyebrow">
                {track.format.toUpperCase()} · {durationOf(track.duration)}
              </span>
              <h2>{titleOf(track)}</h2>
              <p>{textTag(track.tags, "artist") || "Исполнитель не указан"}</p>
              <div className="badge-row">
                <Badge tone={changed ? "warm" : activeProposal ? "purple" : ""}>
                  {changed
                    ? "Есть несохраненные изменения"
                    : activeProposal
                      ? "Предложение сохранено"
                      : "Данные из файла"}
                </Badge>
                {!track.can_write && <Badge>Только чтение</Badge>}
              </div>
            </div>
            <button
              className="icon-button refresh-detail"
              aria-label="Обновить карточку"
              title="Обновить данные файла"
              onClick={onRefresh}
            >
              <RefreshCw size={18} />
            </button>
          </div>
          <div className="workflow-listen">
            <button
              className="button secondary"
              onClick={onPlay}
              disabled={!!track.input_error || track.status === "missing"}
            >
              <AudioLines size={18} />
              Прослушать эту запись
            </button>
            <button className="button subtle" onClick={onSkip} disabled={busy || saving || changed}>
              Пропустить и дальше
              <ArrowRight size={17} />
            </button>
          </div>
          <div className="editor-tabs" role="tablist" aria-label="Редактор трека">
            <button
              role="tab"
              aria-selected={page === "review" && !sourcesOpen}
              onClick={() => {
                setSourcesOpen(false);
                onTab("review");
              }}
            >
              <SlidersHorizontal size={17} />
              Теги и обложка
            </button>
            <button
              role="tab"
              aria-selected={page === "lyrics" && !sourcesOpen}
              onClick={() => {
                setSourcesOpen(false);
                onTab("lyrics");
              }}
            >
              <ListMusic size={18} />
              Текст
            </button>
            <button role="tab" aria-selected={sourcesOpen} onClick={() => setSourcesOpen(true)}>
              <Search size={17} />
              Источники
            </button>
          </div>
          {sourcesOpen && (
            <div className="workflow-sources">
              {!track.possible_mix &&
                !track.can_write &&
                !track.input_error &&
                !track.cue_album_image &&
                !summary?.read_only && (
                  <section className="panel settings-panel">
                    <h2>Редактируемая копия</h2>
                    <p className="muted small">
                      Создать FLAC или PCM WAV для тегов, обложки и текста. Декодированное аудио
                      проверяется по SHA-256. Исходный файл сохраняется; размер копии может
                      увеличиться.
                    </p>
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() => void onJob("normalize_audio")}
                    >
                      Создать редактируемую копию
                    </button>
                  </section>
                )}
              <YouTubePanel
                track={track}
                enabled={!!summary?.capabilities.youtube}
                busy={busy}
                changed={changed}
                onJob={onJob}
                onRefresh={onRefresh}
              />
              <AnalysisPanel
                track={track}
                enabled={!!summary?.capabilities.metadata_search}
                recognitionEnabled={!!summary?.capabilities.recognition}
                asrEnabled={!!summary?.capabilities.asr}
                busy={busy}
                changed={changed}
                onJob={onJob}
                onRefresh={onRefresh}
              />
            </div>
          )}
          {localError && <ErrorBox message={localError} />}
          {track.proposal && !activeProposal && (
            <div className="notice info">
              <RefreshCw size={19} />
              <span>
                Предыдущее предложение относится к другой версии файла. Для новых изменений создайте
                актуальное предложение.
              </span>
            </div>
          )}
          {!writable && (
            <div className="notice warning">
              <LockKeyhole size={19} />
              <span>
                Редактирование этого файла недоступно. Запись зависит от формата, состояния файла и
                проверки хранилища.
              </span>
            </div>
          )}
          <div hidden={sourcesOpen}>
            {page === "review" && (
              <div className="metadata-content">
                <div className="section-intro">
                  <h3>Проверьте метаданные</h3>
                  <Badge tone="purple">Ручное предложение</Badge>
                </div>
                <p className="muted small">
                  Исходные значения остаются видимыми. Сохранение предложения еще не изменяет
                  аудиофайл.
                </p>
                <div className="workflow-field-list">
                  {fields
                    .filter(
                      (field) =>
                        showAllFields ||
                        !activeProposal ||
                        draft[field.key] !== textTag(track.tags, field.key),
                    )
                    .map((field) => (
                      <div
                        key={field.key}
                        className={`workflow-field ${draft[field.key] !== textTag(track.tags, field.key) ? "modified" : ""}`}
                      >
                        <label htmlFor={`tag-${field.key}`}>{field.title}</label>
                        <div className="workflow-field-values">
                          <div>
                            <span>В файле</span>
                            <p>{textTag(track.tags, field.key) || "Не указано"}</p>
                          </div>
                          <ArrowRight size={17} aria-hidden="true" />
                          <div>
                            <label htmlFor={`tag-${field.key}`}>Будет записано</label>
                            <input
                              id={`tag-${field.key}`}
                              value={draft[field.key]}
                              disabled={!writable || saving}
                              onChange={(event) =>
                                setDraft({ ...draft, [field.key]: event.target.value })
                              }
                            />
                          </div>
                        </div>
                        {draft[field.key] !== textTag(track.tags, field.key) && (
                          <button
                            className="text-button"
                            disabled={!writable || saving}
                            onClick={() => {
                              setDraft({ ...draft, [field.key]: textTag(track.tags, field.key) });
                              setShowAllFields(true);
                            }}
                          >
                            Оставить значение из файла
                          </button>
                        )}
                      </div>
                    ))}
                </div>
                <button
                  className="text-button"
                  aria-expanded={showAllFields}
                  onClick={() => setShowAllFields((value) => !value)}
                >
                  {showAllFields ? "Только измененные поля" : "Показать все поля"}
                </button>
                <div className="cover-editor">
                  <div>
                    <h3>Обложка</h3>
                    <p className="muted small">
                      {cover
                        ? "Новое изображение добавлено к черновику."
                        : activeProposal?.cover
                          ? "В предложении сохранена новая обложка. Ниже показана обложка текущего файла."
                          : track.has_art
                            ? "Изображение уже встроено в файл."
                            : "В файле нет обложки."}{" "}
                      Исходник JPEG или PNG до 20 МиБ и 25 мегапикселей.
                    </p>
                  </div>
                  <ArtworkImport
                    trackId={track.id}
                    sha256={track.sha256}
                    selection={slotSelection}
                    current={track.artwork_transform}
                    disabled={!writable || saving}
                    onApply={(value, receipt) => {
                      setCover(value);
                      setArtworkReceipt(receipt);
                    }}
                  />
                </div>
                <div className="notice info">
                  <ShieldCheck size={19} />
                  <span>
                    Остальные теги и изображения сохраняются. Варианты поиска находятся во вкладке
                    «Источники». Таймкоды требуют проверки по аудио.
                  </span>
                </div>
              </div>
            )}
            <details className="workflow-slots">
              <summary>Куда записывать обложку и текст</summary>
              <MediaSlotsPanel
                trackId={track.id}
                sha256={track.sha256}
                selection={slotSelection}
                disabled={!writable || saving}
                onChange={(value) => {
                  setSlotSelection(value);
                }}
                onLoadLyrics={(value) => {
                  try {
                    const parsed = parseLyrics(value);
                    const next = reconcileImportedLines(lyricRows || [], parsed.lines);
                    setLyricRows(next);
                    setLyricMetadata(parsed.metadata);
                    setLrc(serializeLyrics(next));
                  } catch (problem) {
                    setLocalError(messageFor(problem));
                  }
                }}
              />
            </details>
            <p className="quiet-note">
              <AudioLines size={17} />
              Прослушивание и переключение треков доступны в нижней панели.
            </p>
            {page === "lyrics" && (
              <TimingReview
                alignment={track.analysis?.alignment}
                duration={track.duration}
                stale={track.analysis?.alignment?.stale ?? track.analysis?.stale}
                disabled={busy || saving}
                onAcceptLine={acceptTiming}
                onSuggestShift={suggestTimingShift}
                onPreview={
                  audioSupported
                    ? (seconds) => {
                        if (audioRef.current) audioRef.current.currentTime = seconds;
                      }
                    : undefined
                }
              />
            )}
            {page === "lyrics" &&
              (lyricDocument ? (
                <LyricsDocumentEditor
                  value={lrc}
                  lines={lyricRows}
                  metadata={lyricMetadata}
                  onChange={(value, rows, metadata) => {
                    setLrc(value);
                    setLyricRows(rows);
                    if (metadata) setLyricMetadata(metadata);
                  }}
                  disabled={!writable || saving}
                  lockDisabled={changed || busy}
                  onLock={(line, kind) => {
                    void changeLyricLock(line, kind);
                  }}
                  duration={track.duration}
                  currentTime={audioTime}
                  canStamp={audioSupported}
                  onSeek={(seconds) => {
                    if (audioRef.current) audioRef.current.currentTime = seconds;
                  }}
                />
              ) : (
                <LyricsEditor
                  value={lrc}
                  onChange={(value) => {
                    setLrc(value);
                    setLyricRows(undefined);
                  }}
                  disabled={!writable || saving}
                  duration={track.duration}
                  currentTime={audioTime}
                  canStamp={audioSupported}
                  onSeek={(seconds) => {
                    if (audioRef.current) audioRef.current.currentTime = seconds;
                  }}
                />
              ))}
            {hasUntimed && (
              <div className="notice warning">
                <Clock3 size={19} />
                <span>
                  Есть строки без таймкодов. Черновик можно сохранить, но записать
                  синхронизированный текст в файл пока нельзя.
                </span>
              </div>
            )}
            {lyricsInvalid && <ErrorBox message={lyricsInvalid} />}
            <div className="editor-actions">
              <div className="action-hint">
                <ShieldCheck size={17} />
                <span>
                  {changed
                    ? "Сохраните выбранные изменения в черновик"
                    : activeProposal
                      ? "После успешной записи откроется следующий трек"
                      : "Файл остается без изменений"}
                </span>
              </div>
              <div className="button-row">
                <button
                  className="button secondary"
                  onClick={() => void save()}
                  disabled={!writable || !changed || saving || busy}
                >
                  {saving ? (
                    <Spinner label="Сохраняем" />
                  ) : (
                    <>
                      <Save size={18} />
                      Сохранить черновик
                    </>
                  )}
                </button>
                <button
                  className="button primary"
                  onClick={() => void onApplyNext()}
                  disabled={applyBlocked || moving}
                >
                  <CheckCheck size={18} />
                  {moving ? "Записываем..." : "Записать и дальше"}
                </button>
              </div>
            </div>
          </div>
        </div>
        <details className="panel detail-tools">
          <summary>Файл, сохранение результата и история</summary>
          <dl className="file-facts">
            <div>
              <dt>Путь</dt>
              <dd className="break-word">{track.relative_path}</dd>
            </div>
            <div>
              <dt>Ревизия</dt>
              <dd className="mono break-word">{track.sha256}</dd>
            </div>
            <div>
              <dt>Полная проверка файла</dt>
              <dd>
                {track.hash_verified_at
                  ? new Date(track.hash_verified_at).toLocaleString("ru-RU", {
                      dateStyle: "medium",
                      timeStyle: "medium",
                    })
                  : "Дата еще не зафиксирована. Выполните сканирование или проверку аудио."}
              </dd>
            </div>
            <div>
              <dt>Статус</dt>
              <dd>{states[track.status] || track.status}</dd>
            </div>
          </dl>
          <div className="button-row">
            <button
              className="button secondary"
              onClick={() => void onJob("audit")}
              disabled={busy || !summary?.capabilities.audit}
            >
              <Activity size={17} />
              Проверить аудио
            </button>
            <button
              className="button secondary"
              onClick={() => void onJob("publish")}
              disabled={busy || changed || !writable || !summary?.capabilities.publish}
            >
              <FolderCheck size={17} />
              Сохранить в готовую коллекцию
            </button>
            <button
              className="button subtle"
              onClick={() => {
                if (
                  window.confirm(
                    "Восстановить предыдущую сохраненную версию этого файла? Текущий хеш будет проверен перед откатом.",
                  )
                )
                  void onJob("rollback");
              }}
              disabled={
                busy ||
                changed ||
                !writable ||
                !track.history?.some(
                  (item) => item.rollback_available && item.after_sha256 === track.sha256,
                )
              }
            >
              <Clock3 size={17} />
              Откатить запись
            </button>
          </div>
          <p className="muted small">
            Это действие копирует текущую версию в готовую коллекцию. Сохраненное предложение
            сначала нужно записать в файл.
          </p>
          <AudioAuditSummary
            track={track}
            onSeek={
              audioSupported
                ? (seconds) => {
                    const audio = audioRef.current;
                    if (audio) {
                      audio.pause();
                      audio.currentTime = seconds;
                    }
                  }
                : undefined
            }
          />
          {!!track.history?.length && (
            <details>
              <summary>История операций ({track.history.length})</summary>
              <pre className="diagnostic">{JSON.stringify(track.history, null, 2)}</pre>
            </details>
          )}
        </details>
      </div>
      <details className="panel policy-details">
        <summary>Тип записи: песня, инструментальная музыка или сборник</summary>
        <RecordingTypePanel
          key={recordingPanelKey(track)}
          track={track}
          disabled={busy || saving || changed}
          onDirtyChange={setClassificationDirty}
          onChanged={onClassified}
        />
      </details>
    </div>
  );
}

function LyricsEditor({
  value,
  onChange,
  disabled,
  duration,
  currentTime,
  canStamp,
  onSeek,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  duration?: number;
  currentTime: number;
  canStamp: boolean;
  onSeek: (seconds: number) => void;
}) {
  const lastEmitted = useRef(value);
  const initial = useRef<{ lines: LyricLine[]; warnings: string[]; error: string } | null>(null);
  if (!initial.current) {
    try {
      initial.current = { ...parseLyrics(value), error: "" };
    } catch (err) {
      initial.current = { lines: [], warnings: [], error: messageFor(err) };
    }
  }
  const [lines, setLines] = useState<LyricLine[]>(initial.current.lines);
  const [warnings, setWarnings] = useState<string[]>(initial.current.warnings);
  const [error, setError] = useState(initial.current.error);
  const [raw, setRaw] = useState(value);
  const [showRaw, setShowRaw] = useState(!!initial.current.error);
  const [shift, setShift] = useState("0.5");
  const undoStack = useRef<LyricLine[][]>([]);
  const redoStack = useRef<LyricLine[][]>([]);
  const applyLines = (next: LyricLine[]) => {
    setLines(next);
    const serialized = serializeLyrics(next);
    lastEmitted.current = serialized;
    setRaw(serialized);
    onChange(serialized);
    setError("");
  };
  const update = (next: LyricLine[]) => {
    undoStack.current = [...undoStack.current.slice(-99), lines];
    redoStack.current = [];
    applyLines(next);
  };
  function undoLines() {
    const previous = undoStack.current.pop();
    if (previous) {
      redoStack.current.push(lines);
      applyLines(previous);
    }
  }
  function redoLines() {
    const next = redoStack.current.pop();
    if (next) {
      undoStack.current.push(lines);
      applyLines(next);
    }
  }
  useEffect(() => {
    if (value === lastEmitted.current) return;
    lastEmitted.current = value;
    try {
      const parsed = parseLyrics(value);
      undoStack.current = [...undoStack.current.slice(-99), lines];
      redoStack.current = [];
      setLines(parsed.lines);
      setWarnings(parsed.warnings);
      setRaw(value);
      setError("");
    } catch (err) {
      setRaw(value);
      setError(messageFor(err));
    }
  }, [value]);
  function importText(text: string) {
    try {
      const parsed = parseLyrics(text);
      setWarnings(parsed.warnings);
      update(parsed.lines);
      setShowRaw(false);
    } catch (err) {
      setError(messageFor(err));
    }
  }
  function shiftLines(direction: number) {
    const seconds = Number(shift.replace(",", "."));
    if (!Number.isFinite(seconds) || seconds < 0) {
      setError("Укажите положительный сдвиг в секундах.");
      return;
    }
    const next = lines.map((line) => ({
      ...line,
      time: line.time === null ? null : Math.round(line.time + direction * seconds * 1000),
    }));
    const validation = validateLyrics(next, duration);
    if (validation) setError(validation);
    else update(next);
  }
  function move(index: number, delta: number) {
    const next = [...lines];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    update(next);
  }
  const active = lines.reduce(
    (result, line, index) =>
      line.time !== null && line.time <= currentTime * 1000 ? index : result,
    -1,
  );
  return (
    <div className="lyrics-editor">
      <div className="section-intro">
        <div>
          <h3>Синхронизированный текст</h3>
          <p className="muted small">
            {lines.filter((line) => line.time !== null).length} из {lines.length} строк с временем
          </p>
        </div>
        <div className="button-row">
          <button
            className="button subtle"
            disabled={disabled || !undoStack.current.length || showRaw}
            onClick={undoLines}
          >
            Отменить
          </button>
          <button
            className="button subtle"
            disabled={disabled || !redoStack.current.length || showRaw}
            onClick={redoLines}
          >
            Повторить
          </button>
          <LyricsImport disabled={disabled} onImport={importText} />
          <button className="button subtle" onClick={() => setShowRaw(!showRaw)}>
            <BookOpen size={17} />
            {showRaw ? "Строки" : "Вставить текст"}
          </button>
        </div>
      </div>
      {error && <ErrorBox message={error} />}
      {warnings.map((warning) => (
        <p className="small warning-text" key={warning}>
          {warning}
        </p>
      ))}
      {showRaw ? (
        <div className="raw-editor">
          <label>
            Текст или LRC
            <textarea
              value={raw}
              disabled={disabled}
              onChange={(event) => {
                lastEmitted.current = event.target.value;
                setRaw(event.target.value);
                onChange(event.target.value);
              }}
              placeholder="[00:12.500]Первая строка"
              rows={12}
            />
          </label>
          <button className="button secondary" disabled={disabled} onClick={() => importText(raw)}>
            Применить к черновику
          </button>
          <p className="muted small">
            При импорте положительный offset вычитается из времени строки. Например, 10.000 с с
            offset +500 станет 9.500 с.
          </p>
        </div>
      ) : (
        <>
          <div className="lyrics-toolbar">
            <span className="muted small">Сдвинуть все таймкоды</span>
            <div className="shift-controls">
              <button
                className="icon-button"
                disabled={disabled || !lines.length}
                onClick={() => shiftLines(-1)}
                title="Сдвинуть раньше"
                aria-label="Сдвинуть таймкоды раньше"
              >
                <ArrowLeft size={17} />
              </button>
              <input
                aria-label="Размер сдвига в секундах"
                inputMode="decimal"
                value={shift}
                onChange={(event) => setShift(event.target.value)}
              />
              <span>с</span>
              <button
                className="icon-button"
                disabled={disabled || !lines.length}
                onClick={() => shiftLines(1)}
                title="Сдвинуть позже"
                aria-label="Сдвинуть таймкоды позже"
              >
                <ArrowRight size={17} />
              </button>
            </div>
          </div>
          <div className="lyric-lines">
            {lines.length === 0 ? (
              <div className="empty compact">
                <ListMusic size={30} />
                <p>
                  Добавьте текст или импортируйте LRC.
                  <br />
                  Строки без времени останутся черновиком.
                </p>
              </div>
            ) : (
              lines.map((line, index) => (
                <div className={`lyric-line ${active === index ? "current" : ""}`} key={line.id}>
                  <span className="line-index">{String(index + 1).padStart(2, "0")}</span>
                  <TimeInput
                    time={line.time}
                    disabled={disabled}
                    index={index}
                    onChange={(time) =>
                      update(lines.map((item, i) => (i === index ? { ...item, time } : item)))
                    }
                    onError={setError}
                  />
                  <button
                    className="icon-button stamp-button"
                    aria-label={`Отметить строку ${index + 1} текущим временем аудио`}
                    title={canStamp ? "Текущее время аудио" : "Сначала загрузите аудио"}
                    disabled={disabled || !canStamp}
                    onClick={() =>
                      update(
                        lines.map((item, i) =>
                          i === index ? { ...item, time: Math.round(currentTime * 1000) } : item,
                        ),
                      )
                    }
                  >
                    <Clock3 size={16} />
                  </button>
                  <input
                    className="lyric-text"
                    aria-label={`Текст строки ${index + 1}`}
                    value={line.text}
                    disabled={disabled}
                    onChange={(event) =>
                      update(
                        lines.map((item, i) =>
                          i === index ? { ...item, text: event.target.value } : item,
                        ),
                      )
                    }
                    placeholder="Пустая строка"
                  />
                  <div className="line-actions">
                    <button
                      className="icon-button"
                      aria-label={`Перейти к строке ${index + 1}`}
                      disabled={!canStamp || line.time === null}
                      onClick={() => onSeek((line.time || 0) / 1000)}
                    >
                      <Music2 size={16} />
                    </button>
                    <button
                      className="icon-button"
                      aria-label={`Переместить строку ${index + 1} вверх`}
                      disabled={disabled || index === 0}
                      onClick={() => move(index, -1)}
                    >
                      <ArrowUp size={15} />
                    </button>
                    <button
                      className="icon-button"
                      aria-label={`Переместить строку ${index + 1} вниз`}
                      disabled={disabled || index === lines.length - 1}
                      onClick={() => move(index, 1)}
                    >
                      <ArrowDown size={15} />
                    </button>
                    <button
                      className="icon-button"
                      aria-label={`Удалить строку ${index + 1}`}
                      disabled={disabled}
                      onClick={() => update(lines.filter((_, i) => i !== index))}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
          <button
            className="button subtle"
            disabled={disabled}
            onClick={() => update([...lines, { id: crypto.randomUUID(), time: null, text: "" }])}
          >
            <Plus size={17} />
            Добавить строку
          </button>
        </>
      )}
      <div className="quiet-note">
        <AudioLines size={17} />
        Таймкоды относятся к полному файлу. Задержка Bluetooth не добавляется к ним автоматически.
      </div>
    </div>
  );
}
function TimeInput({
  time,
  index,
  disabled,
  onChange,
  onError,
}: {
  time: number | null;
  index: number;
  disabled: boolean;
  onChange: (time: number | null) => void;
  onError: (message: string) => void;
}) {
  const [value, setValue] = useState(formatTime(time));
  useEffect(() => setValue(formatTime(time)), [time]);
  return (
    <input
      className="time-input mono"
      aria-label={`Время строки ${index + 1}`}
      placeholder="Без времени"
      value={value}
      disabled={disabled}
      onChange={(event) => setValue(event.target.value)}
      onBlur={() => {
        try {
          const next = parseTime(value);
          if (next !== time) onChange(next);
        } catch (err) {
          onError(messageFor(err));
          setValue(formatTime(time));
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
      }}
    />
  );
}

function SettingsView({
  summary,
  theme,
  setTheme,
  onDirtyChange,
  onLogoutAll,
}: {
  summary: Summary | null;
  theme: string;
  setTheme: (value: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onLogoutAll: () => void;
}) {
  return (
    <div className="settings-grid">
      <OperationsPanel />
      <ProvidersPanel />
      <ProviderPolicyPanel />
      <SourcePriorityPanel />
      <SchedulePanel />
      <AutomationPanel />
      <WebhooksPanel />
      <RetentionPanel onDirtyChange={onDirtyChange} />
      <section className="panel settings-panel">
        <div className="section-icon">
          <Settings2 size={21} />
        </div>
        <h2>Ваш экземпляр</h2>
        <button className="button secondary" onClick={onLogoutAll}>
          <LogOut size={17} />
          Выйти на всех устройствах
        </button>
        <p className="muted small">
          Завершает все сеансы владельца. Уже одобренные задачи и отдельные API-токены сохраняются;
          токены отзываются ниже в автоматизации.
        </p>
        <dl className="file-facts">
          <div>
            <dt>Версия</dt>
            <dd>{summary?.version || "Нет данных"}</dd>
          </div>
          <div>
            <dt>Доступ к файлам</dt>
            <dd>
              {summary
                ? summary.read_only
                  ? "Только чтение"
                  : "Рабочая копия: запись разрешена"
                : "Нет данных"}
            </dd>
          </div>
          <div>
            <dt>Обработчик</dt>
            <dd>
              {summary?.worker?.state
                ? states[summary.worker.state] || summary.worker.state
                : "Нет данных"}
            </dd>
          </div>
          <div>
            <dt>Последний сигнал</dt>
            <dd>{dateOf(summary?.worker.last_seen)}</dd>
          </div>
          {summary?.roots?.work && (
            <div>
              <dt>Рабочая коллекция</dt>
              <dd className="break-word">{summary.roots.work}</dd>
            </div>
          )}
        </dl>
        <div className="settings-field">
          <label htmlFor="theme">Оформление</label>
          <select id="theme" value={theme} onChange={(event) => setTheme(event.target.value)}>
            <option value="system">Как в системе</option>
            <option value="light">Светлое</option>
            <option value="dark">Темное</option>
          </select>
        </div>
        <p className="muted small">
          Пути, ресурсы обработчика и учетная запись владельца задаются в конфигурации Docker.
        </p>
      </section>
      <section className="panel settings-panel">
        <div className="section-icon purple">
          <WandSparkles size={21} />
        </div>
        <h2>Возможности</h2>
        <div className="capability-list">
          {[
            { label: "Каталог и проверка аудио", ready: summary?.capabilities.audit },
            { label: "Ручные предложения тегов и LRC", ready: summary?.capabilities.edit },
            {
              label: "Запись MP3 / FLAC / M4A / OGG / OPUS / WAV / AIFF",
              ready: summary?.capabilities.apply,
            },
            { label: "Публикация готовой коллекции", ready: summary?.capabilities.publish },
            { label: "Поиск метаданных и текстов", ready: summary?.capabilities.metadata_search },
            { label: "Распознавание ShazamIO", ready: summary?.capabilities.recognition },
            { label: "ASR и выравнивание текста", ready: summary?.capabilities.asr },
          ].map((item) => (
            <div key={item.label}>
              <span>{item.label}</span>
              <Badge tone={item.ready ? "good" : ""}>
                {item.ready ? "Доступно" : "Недоступно"}
              </Badge>
            </div>
          ))}
        </div>
        <p className="muted small">
          MusicBrainz, LRCLIB и Cover Art Archive подготавливают предложения. ShazamIO распознает
          локально подготовленные аудиоотпечатки без API-ключа, если отправка включена в
          конфигурации. Платные запросы отключены.
        </p>
      </section>
      <section className="panel settings-panel settings-wide">
        <div className="section-icon">
          <ShieldCheck size={21} />
        </div>
        <h2>Три отдельных результата</h2>
        <div className="safety-columns">
          <div>
            <span className="step-number">01</span>
            <h3>Рабочая копия</h3>
            <p>
              Изменения записываются после сохранения предложения. Предыдущая версия нужна для
              отката.
            </p>
          </div>
          <div>
            <span className="step-number">02</span>
            <h3>Готовая коллекция</h3>
            <p>
              Публикация создает отдельную копию принятой версии. Она еще не означает доставку на
              телефон.
            </p>
          </div>
          <div>
            <span className="step-number">03</span>
            <h3>Poweramp</h3>
            <p>
              Проверьте теги, обложку, подсветку и перемотку офлайн. Проверка относится к конкретной
              версии файла.
            </p>
          </div>
        </div>
        <div className="notice info">
          <CircleAlert size={19} />
          <span>
            Резервные версии на том же хранилище не заменяют независимый backup музыкальной
            коллекции.
          </span>
        </div>
      </section>
    </div>
  );
}
