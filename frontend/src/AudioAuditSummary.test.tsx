import { Children, isValidElement } from "react";
import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import AudioAuditSummary from "./AudioAuditSummary";
import type { Track } from "./types";

const sha = "a".repeat(64);
const track: Track = {
  id: "fixture-002",
  revision: sha,
  sha256: sha,
  relative_path: "private/002.wav",
  format: "wav",
  tags: {},
  has_art: false,
  status: "indexed",
  can_write: true,
};
const render = (extra: Partial<Track>) =>
  renderToStaticMarkup(<AudioAuditSummary track={{ ...track, ...extra }} />);
const interval = { start_seconds: 5, end_seconds: 11.5, duration_seconds: 6.5 };
const measuredAudit = {
  state: "passed",
  sha256: sha,
  duration: 30,
  warnings: [{ code: "long_silence" }],
  signal: {
    state: "measured",
    method: "ffmpeg_signal_v1",
    silence_intervals: [interval],
    silence_intervals_truncated: false,
  },
};
const withSignal = (patch: Record<string, unknown>) => ({
  ...measuredAudit,
  signal: { ...measuredAudit.signal, ...patch },
});
function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...Children.toArray(node.props.children as ReactNode).flatMap(elements)];
}

describe("human audio audit result", () => {
  it("shows verified current-revision silence boundaries without inventing a general damage location", () => {
    const markup = render({ duration: 30, audit_status: "warning", audit: measuredAudit });
    expect(markup).toContain("Тихие участки");
    expect(markup).toContain("00:05.000 - 00:11.500");
    expect(markup).toContain("Границы тихих участков показаны ниже");
    expect(markup).toContain("У других предупреждений временные границы могут отсутствовать");
    expect(markup).not.toContain("Неизвестен: временные границы");
    expect(markup).not.toContain("К началу участка");
  });

  it.each<{ name: string; changes: Partial<Track> }>([
    { name: "stale revision", changes: { audit: { ...measuredAudit, sha256: "b".repeat(64) } } },
    { name: "unmeasured signal", changes: { audit: withSignal({ state: "unavailable" }) } },
    { name: "unknown signal method", changes: { audit: withSignal({ method: "unknown" }) } },
    {
      name: "NaN start",
      changes: { audit: withSignal({ silence_intervals: [{ ...interval, start_seconds: NaN }] }) },
    },
    {
      name: "infinite end",
      changes: {
        audit: withSignal({ silence_intervals: [{ ...interval, end_seconds: Infinity }] }),
      },
    },
    {
      name: "negative start",
      changes: { audit: withSignal({ silence_intervals: [{ ...interval, start_seconds: -1 }] }) },
    },
    {
      name: "reversed range",
      changes: { audit: withSignal({ silence_intervals: [{ ...interval, start_seconds: 12 }] }) },
    },
    { name: "track duration boundary", changes: { duration: 10 } },
    { name: "zero duration boundary", changes: { duration: 0 } },
    { name: "audit duration boundary", changes: { audit: { ...measuredAudit, duration: 10 } } },
    {
      name: "unknown duration bounds",
      changes: { duration: undefined, audit: { ...measuredAudit, duration: undefined } },
    },
    {
      name: "inconsistent interval duration",
      changes: {
        audit: withSignal({ silence_intervals: [{ ...interval, duration_seconds: 19 }] }),
      },
    },
    {
      name: "nonfinite interval duration",
      changes: {
        audit: withSignal({ silence_intervals: [{ ...interval, duration_seconds: NaN }] }),
      },
    },
    {
      name: "current failed verdict",
      changes: { audit_status: "error", audit_error: "invalid_audio" },
    },
  ])("does not expose unverified intervals or a seek control for $name", ({ changes }) => {
    const markup = renderToStaticMarkup(
      <AudioAuditSummary
        track={{
          ...track,
          duration: 30,
          audit_status: "warning",
          audit: measuredAudit,
          ...changes,
        }}
        onSeek={vi.fn()}
      />,
    );
    expect(markup).not.toContain("<h3>Тихие участки");
    expect(markup).not.toContain("К началу участка");
    expect(markup).toContain("Неизвестен: временные границы не зафиксированы");
    expect(markup).not.toContain("NaN");
    expect(markup).not.toContain("Infinity");
  });

  it("bounds the interval list to100 and explicitly reports both oversized input and the server truncation flag", () => {
    const intervals = Array.from({ length: 103 }, (_, index) => ({
      start_seconds: index * 6,
      end_seconds: index * 6 + 5,
      duration_seconds: 5,
    }));
    const markup = renderToStaticMarkup(
      <AudioAuditSummary
        track={{
          ...track,
          duration: 1000,
          audit_status: "warning",
          audit: { ...withSignal({ silence_intervals: intervals }), duration: 1000 },
        }}
        onSeek={vi.fn()}
      />,
    );
    expect(markup.match(/aria-label="Перейти к тихому участку/g)).toHaveLength(100);
    expect(markup).toContain("Список ограничен 100 интервалами");
    expect(markup).toContain("09:54.000 - 09:59.000");
    expect(markup).not.toContain("10:00.000 - 10:05.000");
    const flagged = render({
      duration: 30,
      audit_status: "warning",
      audit: withSignal({ silence_intervals_truncated: true }),
    });
    expect(flagged).toContain("Показано интервалов: 1");
    expect(flagged).toContain("в записи могут быть и другие тихие участки");
  });

  it("invokes only the supplied seek callback at the validated start with a native keyboard-focusable button", () => {
    const onSeek = vi.fn();
    const tree = AudioAuditSummary({
      track: { ...track, duration: 30, audit_status: "warning", audit: measuredAudit },
      onSeek,
    });
    const buttons = elements(tree).filter((node) => node.type === "button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props["aria-label"]).toContain("Перейти к тихому участку 1, 00:05.000");
    expect(buttons[0].props).not.toHaveProperty("tabIndex", -1);
    (buttons[0].props.onClick as () => void)();
    expect(onSeek.mock.calls).toEqual([[5]]);
    expect(renderToStaticMarkup(tree)).toContain("переход к участку не запускает воспроизведение");
  });

  it("distinguishes a resource refusal from invalid audio and never invents a damaged timestamp", () => {
    const limited = render({
      audit_status: "error",
      audit_error: "verification_too_large",
      audit_stage: "packets",
    });
    expect(limited).toContain("Проверка не завершена");
    expect(limited).toContain("Это не доказательство повреждения аудио");
    expect(limited).toContain("Проверка аудиопакетов");
    expect(limited).toContain("Неизвестен: временные границы не зафиксированы");
    expect(limited).not.toContain("00:00");
    const invalid = render({
      audit_status: "error",
      audit_error: "invalid_audio",
      audit_stage: "decode",
    });
    expect(invalid).toContain("Ошибка проверки аудио");
    expect(invalid).toContain("Полное декодирование аудио");
    expect(invalid).not.toContain("Достигнут безопасный лимит");
  });

  it("renders the server current-revision verdict instead of a historical raw PASS", () => {
    const historical = {
      state: "passed",
      sha256: "b".repeat(64),
      warnings: [{ code: "possible_clipping" }],
      error: "old_error",
      failure_stage: "decode",
    };
    const stale = render({
      audit_status: "unchecked",
      audit_error: "",
      audit_stage: "",
      audit: historical,
    });
    expect(stale).toContain("Проверка устарела");
    expect(stale).toContain("Не подтверждена");
    expect(stale).not.toContain("Возможен клиппинг");
    expect(stale).not.toContain("old_error");
    const missing = render({
      status: "missing",
      audit_status: "error",
      audit_error: "missing_file",
      audit_stage: "",
      audit: historical,
    });
    expect(missing).toContain("Файл недоступен");
    expect(missing).not.toContain("Проверка устарела");
    const unreadable = render({
      audit_status: "error",
      audit_error: "invalid_audio",
      audit_stage: "",
      input_error: { code: "invalid_audio", message: "SECRET_CURRENT_ERROR" },
      audit: { state: "passed", sha256: sha, warnings: [] },
    });
    expect(unreadable).toContain("Ошибка проверки аудио");
    expect(unreadable).toContain("Не подтверждена");
    expect(unreadable).not.toContain("SECRET_CURRENT_ERROR");
  });

  it.each([0, 1])(
    "explains the saved %s-byte WebM input error without reviving its historical audit stage",
    (size) => {
      const markup = render({
        format: "webm",
        size,
        status: "error",
        audit_status: "error",
        audit_error: "invalid_audio",
        audit_stage: "",
        input_error: { code: "invalid_audio", message: "SECRET_CURRENT_ERROR" },
        audit: { state: "error", sha256: sha, error: "invalid_audio", failure_stage: "header" },
      });
      expect(markup).toContain(`Размер при сканировании: ${size} Б`);
      expect(markup).toContain("сохранённого размера недостаточно для полноценного WebM-аудио");
      expect(markup).toContain("Нужен исходный файл или повторное скачивание");
      expect(markup).toContain("Изменение тегов не восстановит аудио");
      expect(markup).toContain("Не подтверждена");
      expect(markup).toContain("<dt>Этап</dt><dd>Не зафиксирован</dd>");
      expect(markup).not.toContain("Чтение формата и аудиопотоков");
      expect(markup).not.toContain("Аудио проверено");
      expect(markup).not.toContain("SECRET_CURRENT_ERROR");
      expect(markup).not.toContain("<button");
    },
  );

  it.each([
    { name: "missing size", changes: { size: undefined } },
    { name: "negative size", changes: { size: -1 } },
    { name: "fractional size", changes: { size: 0.5 } },
    { name: "nonfinite size", changes: { size: NaN } },
    { name: "oversized integer", changes: { size: Number.MAX_SAFE_INTEGER + 1 } },
    { name: "larger WebM", changes: { size: 2 } },
    { name: "another format", changes: { format: "mp3" } },
    { name: "unchecked current input", changes: { audit_status: "unchecked" } },
    { name: "missing current input", changes: { status: "missing", audit_error: "missing_file" } },
    { name: "different current error", changes: { audit_error: "input_error" } },
    { name: "successful current audit", changes: { audit_status: "passed" } },
  ] satisfies { name: string; changes: Partial<Track> }[])(
    "does not infer incomplete WebM audio from $name",
    ({ changes }) => {
      const markup = render({
        format: "webm",
        size: 1,
        audit_status: "error",
        audit_error: "invalid_audio",
        audit_stage: "",
        audit: { state: "error", sha256: "b".repeat(64), failure_stage: "header" },
        ...changes,
      });
      expect(markup).not.toContain("размера недостаточно для полноценного WebM-аудио");
      expect(markup).not.toContain("Нужен исходный файл или повторное скачивание");
      expect(markup).not.toContain("Изменение тегов не восстановит аудио");
      expect(markup).not.toContain("Чтение формата и аудиопотоков");
    },
  );

  it("shows current passed warnings and notes without turning them into a clean or musical approval", () => {
    const markup = render({
      audit_status: "warning",
      audit: {
        state: "passed",
        sha256: sha,
        warnings: [{ code: "multiple_audio_streams", message: "private/path.wav" }],
        notes: [{ code: "decoder_channel_layout_inferred" }],
        audio_stream_count: 2,
      },
    });
    expect(markup).toContain("Проверено с предупреждениями");
    expect(markup).toContain("Проверены все аудиопотоки");
    expect(markup).toContain("Примечания декодера");
    expect(markup).toContain("не проверка музыкального содержания");
    expect(markup).toContain("включает записи с предупреждениями");
    expect(markup).not.toContain("private/path.wav");
  });

  it("keeps absent/malformed legacy results unchecked and explains CUE as a separate physical-audio scope", () => {
    const legacy = render({ audit: { state: "passed", sha256: sha, warnings: "malformed" } });
    expect(legacy).toContain("Нет актуального результата");
    expect(legacy).not.toContain("Аудио проверено");
    const cue = render({ format: "cue", audit_status: "not_applicable" });
    expect(cue).toContain("CUE не является аудиофайлом");
    expect(cue).toContain("Проверять нужно связанные аудиофайлы");
    const image = render({
      cue_album_image: true,
      audit_status: "passed",
      audit: { state: "passed", sha256: sha, warnings: [] },
    });
    expect(image).toContain("Аудио проверено");
    expect(image).not.toContain("CUE не является");
  });

  it("retains safe diagnostics in a closed disclosure while excluding messages, stderr, paths and unknown payloads", () => {
    const markup = render({
      audit_status: "error",
      audit_error: "unsupported_format",
      audit_stage: "",
      audit: {
        sha256: sha,
        state: "unsupported",
        stderr: "SECRET_STDERR /private/music.wav",
        message: "SECRET_MESSAGE",
        audio_streams: [{ codec_name: "SECRET_STREAM" }],
        nested: { token: "SECRET_TOKEN" },
      },
    });
    expect(markup).toContain("Формат не поддерживается");
    expect(markup).toContain("<details><summary>Диагностические данные проверки");
    expect(markup).not.toContain("<details open");
    expect(markup).toContain("unsupported_format");
    for (const unsafe of [
      "SECRET_STDERR",
      "SECRET_MESSAGE",
      "SECRET_STREAM",
      "SECRET_TOKEN",
      "/private/music.wav",
    ])
      expect(markup).not.toContain(unsafe);
    const unknown = render({
      audit_status: "error",
      audit_error: "decoder:unrecognized-profile",
      audit_stage: "header",
    });
    expect(unknown).toContain("decoder:unrecognized-profile");
    expect(unknown).toContain("Чтение формата и аудиопотоков");
  });
});
