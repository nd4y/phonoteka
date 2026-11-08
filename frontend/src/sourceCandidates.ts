import type { Candidate } from "./types";

export const sourceNames: Record<string, string> = {
  musicbrainz: "MusicBrainz",
  lrclib: "LRCLIB",
  shazam: "Shazam",
  acoustid: "AcoustID",
  coverartarchive: "Cover Art Archive",
  local_cover: "Локальная обложка",
  local_replaygain: "Измерение громкости",
  discogs: "Discogs",
  theaudiodb: "TheAudioDB",
};
export const candidateGroups: Record<string, { title: string; help: string }> = {
  metadata: {
    title: "Теги песни",
    help: "Выберите свою версию записи. Совпадение по аудио помогает узнать песню, но не подтверждает альбом.",
  },
  release: {
    title: "Альбом и издание",
    help: "Сверьте альбом, год и состав треков. Концертная и студийная записи могут называться одинаково.",
  },
  lyrics: {
    title: "Текст песни",
    help: "Сверьте слова по аудио. Для варианта без таймкодов потребуется отдельное выравнивание.",
  },
  cover: {
    title: "Обложка",
    help: "Выберите обложку именно этого альбома или издания.",
  },
  replaygain: {
    title: "Громкость",
    help: "Измерение запишет теги ReplayGain, аудиосэмплы не изменятся.",
  },
};
export const candidateText = (value: unknown): string =>
  Array.isArray(value) ? value.join(", ") : String(value ?? "");
export function candidateDuration(value?: number): string {
  if (value == null || !Number.isFinite(value)) return "Не указана";
  const seconds = Math.round(value);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
export function lyricsSummary(value?: string): string {
  if (!value?.trim()) return "Нет текста";
  const lines = value
    .split(/\r?\n/)
    .filter((line) => line.trim() && !/^\[(?:ar|al|ti|by|offset):/i.test(line));
  const timed = lines.filter((line) => /^\[\d+:\d+(?:[.:]\d+)?\]/.test(line)).length;
  return `${timed === 0 ? "Без таймкодов" : timed === lines.length ? "С таймкодами" : "Часть строк с таймкодами"} · строк: ${lines.length}`;
}
export function groupCandidates(candidates: Candidate[]) {
  const kinds = [...new Set([...Object.keys(candidateGroups), ...candidates.map((c) => c.kind)])];
  return kinds
    .map((kind) => ({
      kind,
      ...(candidateGroups[kind] || {
        title: "Другие данные",
        help: "Проверьте предлагаемые поля перед выбором.",
      }),
      candidates: candidates.filter((c) => c.kind === kind),
    }))
    .filter((group) => group.candidates.length);
}
export function chooseCandidate(
  chosen: string[],
  candidates: Candidate[],
  kind: string,
  id?: string,
): string[] {
  return [
    ...chosen.filter((selected) => candidates.some((c) => c.id === selected && c.kind !== kind)),
    ...(id ? [id] : []),
  ];
}
